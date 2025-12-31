import re
import time
import html
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, unquote
from email.utils import parsedate_to_datetime

import requests
import feedparser
from feedgen.feed import FeedGenerator


# -----------------------
# Configuration
# -----------------------

SUBREDDITS = [
    "medicine",
    "emergencymedicine",
    "FamilyMedicine",
    "InternalMedicine",
    "criticalcare",
    "Psychiatry",
    "Anesthesiology",
    "Radiology",
    "neurology",
    "ophthalmology",
]

POST_LIMIT = 50
MAX_ITEMS = 250  # cap total RSS items

HEADERS_REDDIT = {
    "User-Agent": "jd261-marit-reddit-newsfeed/0.4 (RSS aggregation, personal use)"
}

HEADERS_FETCH = {
    "User-Agent": "Mozilla/5.0 (compatible; MaritRedditNewsfeed/0.4; +https://jd261.github.io/marit-reddit-newsfeed/)"
}

DROP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid",
}

# Treat these as "Reddit-owned" so we don't emit them as outbound items
REDDIT_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "redd.it",
    "i.redd.it",
    "v.redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
    "redditmedia.com",
    "www.redditmedia.com",
    "b.thumbs.redditmedia.com",
}

# Things we do not want in a "news/blog" feed
BLOCKED_HOST_SUFFIXES = {
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "fb.com",
    "discord.gg",
    "discord.com",
    "docs.google.com",
    "drive.google.com",
    "forms.gle",
    "subredditstats.com",
}

BLOCKED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".mp4", ".mov", ".m4v", ".avi",
    ".pdf", ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx",
    ".zip", ".rar", ".7z",
}

# Extract hrefs from HTML in RSS summaries
HREF_RE = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)
URL_RE_FALLBACK = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)

# Extract <title> and common OG titles
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL
)
TW_TITLE_RE = re.compile(
    r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL
)


# -----------------------
# URL helpers
# -----------------------

def normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip(').,]>"\'')
    try:
        p = urlparse(raw)
        if not p.scheme or not p.netloc:
            return raw

        q = [
            (k, v)
            for (k, v) in parse_qsl(p.query, keep_blank_values=True)
            if k not in DROP_QUERY_KEYS
        ]

        return p._replace(query=urlencode(q)).geturl()
    except Exception:
        return raw


def is_reddit_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host in REDDIT_HOSTS or host.endswith(".reddit.com")
    except Exception:
        return False


def unwrap_reddit_media(url: str) -> str:
    # Converts https://www.reddit.com/media?url=<ENCODED_URL> into the real outbound URL
    try:
        p = urlparse(url)
        if p.netloc.lower().endswith("reddit.com") and p.path == "/media":
            qs = dict(parse_qsl(p.query))
            if "url" in qs and qs["url"]:
                return normalize_url(unquote(qs["url"]))
    except Exception:
        pass
    return url


def looks_like_news_or_blog_url(url: str) -> bool:
    """
    Heuristic filter:
    - Must be http(s)
    - Not Reddit-owned
    - Not obviously an image/video/doc file
    - Not obviously a social/video platform or docs/forms
    """
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False

        host = p.netloc.lower()
        if is_reddit_host(url):
            return False

        for suf in BLOCKED_HOST_SUFFIXES:
            if host == suf or host.endswith("." + suf):
                return False

        path = (p.path or "").lower()
        for ext in BLOCKED_EXTENSIONS:
            if path.endswith(ext):
                return False

        # drop obvious "comment threads" and "permalink" style pages on reddit mirrors
        if "/comments/" in path and ("reddit" in host):
            return False

        return True
    except Exception:
        return False


# -----------------------
# Fetch and parse Reddit RSS
# -----------------------

def fetch_subreddit_rss(subreddit: str):
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?ts={int(time.time())}"
    r = requests.get(url, headers=HEADERS_REDDIT, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.text).entries[:POST_LIMIT]


def extract_external_links(entry):
    """
    Pull outbound href targets from the RSS summary HTML.
    This avoids the '<link>... "&gt; ...</link>' pollution you saw.
    """
    links = set()

    summary = getattr(entry, "summary", "") or ""

    # Prefer href="..." extraction (cleanest)
    for href in HREF_RE.findall(summary):
        href = html.unescape(href)
        href = normalize_url(unwrap_reddit_media(href))
        if looks_like_news_or_blog_url(href):
            links.add(href)

    # Fallback: if no hrefs, try a safer URL regex
    if not links:
        for u in URL_RE_FALLBACK.findall(summary):
            u = html.unescape(u)
            u = normalize_url(unwrap_reddit_media(u))
            if looks_like_news_or_blog_url(u):
                links.add(u)

    return links


# -----------------------
# Fetch title from external pages
# -----------------------

def clean_title(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"\s+", " ", t).strip()
    # Remove common junk wrappers
    t = t.strip(" \t\r\n-–|")
    return t


def fetch_page_title(url: str) -> str | None:
    """
    Best effort title fetch:
    - HEAD to validate content-type (some servers block HEAD, so we fall back)
    - GET first ~200KB of HTML and parse og:title, twitter:title, then <title>
    """
    try:
        # Some sites block HEAD, so errors are ok
        try:
            h = requests.head(url, headers=HEADERS_FETCH, timeout=12, allow_redirects=True)
            ctype = (h.headers.get("Content-Type") or "").lower()
            if ctype and ("text/html" not in ctype):
                return None
        except Exception:
            pass

        r = requests.get(url, headers=HEADERS_FETCH, timeout=18, allow_redirects=True)
        r.raise_for_status()

        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype and ("text/html" not in ctype):
            return None

        # Keep it lightweight
        text = r.text
        if len(text) > 200_000:
            text = text[:200_000]

        m = OG_TITLE_RE.search(text)
        if m:
            t = clean_title(m.group(1))
            if t:
                return t

        m = TW_TITLE_RE.search(text)
        if m:
            t = clean_title(m.group(1))
            if t:
                return t

        m = TITLE_TAG_RE.search(text)
        if m:
            t = clean_title(m.group(1))
            if t:
                return t

        return None
    except Exception:
        return None


# -----------------------
# RSS generation
# -----------------------

def build_rss(items):
    fg = FeedGenerator()
    fg.id("reddit:medicine:external-links")
    fg.title("External links from medicine subreddits")
    fg.description("Outbound articles and blog posts shared across selected medicine-related subreddits.")
    fg.link(href="rss.xml", rel="self")
    fg.updated(datetime.now(timezone.utc))

    for it in items:
        fe = fg.add_entry()
        fe.id(it["guid"])
        fe.title(it["title"])
        fe.link(href=it["link"])
        fe.published(it["published"])
        fe.description(it["description"])

    return fg.rss_str(pretty=True)


def main():
    seen = set()
    items = []

    for sub in SUBREDDITS:
        entries = fetch_subreddit_rss(sub)
        time.sleep(2)

        for e in entries:
            reddit_post_title = getattr(e, "title", f"Post from r/{sub}")
            reddit_link = getattr(e, "link", "")

            try:
                published = parsedate_to_datetime(e.published).astimezone(timezone.utc)
            except Exception:
                published = datetime.now(timezone.utc)

            outbound_links = list(extract_external_links(e))

            for link in outbound_links:
                # Title from the destination page (news/blog headline)
                article_title = fetch_page_title(link)

                # If we cannot reliably get a title, skip it.
                # This is part of "filter out anything that's not news/blog"
                if not article_title:
                    continue

                # Extra filter: reject "empty" titles and obvious non-articles
                if len(article_title) < 12:
                    continue

                guid = hashlib.sha256(f"{sub}|{link}".encode()).hexdigest()
                if guid in seen:
                    continue
                seen.add(guid)

                items.append({
                    "guid": guid,
                    "title": article_title,
                    "link": link,
                    "published": published,
                    "description": f"Shared on r/{sub} | Reddit post: {reddit_link} | Reddit title: {reddit_post_title}",
                })

                if len(items) >= MAX_ITEMS:
                    break

            if len(items) >= MAX_ITEMS:
                break

        if len(items) >= MAX_ITEMS:
            break

    items.sort(key=lambda x: x["published"], reverse=True)
    rss = build_rss(items)

    with open("rss.xml", "wb") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
