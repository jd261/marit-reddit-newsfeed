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

# IMPORTANT: set this to your real published feed URL
FEED_URL = "https://jd261.github.io/marit-reddit-newsfeed/rss.xml"

HEADERS_REDDIT = {
    "User-Agent": "jd261-marit-reddit-newsfeed/0.5 (RSS aggregation, personal use)"
}

HEADERS_FETCH = {
    "User-Agent": "Mozilla/5.0 (compatible; MaritRedditNewsfeed/0.5; +https://jd261.github.io/marit-reddit-newsfeed/)"
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

# Detect "junk" titles from bot protection, paywalls, consent pages, etc.
JUNK_TITLE_PATTERNS = [
    r"just a moment",
    r"checking your browser",
    r"verify you are human",
    r"access denied",
    r"permission denied",
    r"request blocked",
    r"service unavailable",
    r"temporarily unavailable",
    r"enable javascript",
    r"cookies are required",
    r"consent",
    r"subscribe to continue",
    r"sign in to continue",
]
JUNK_TITLE_RE = re.compile("|".join(JUNK_TITLE_PATTERNS), re.IGNORECASE)


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

        scheme = p.scheme
        if scheme in ("http", "https"):
            scheme = "https"  # prefer https

        return p._replace(scheme=scheme, query=urlencode(q)).geturl()
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
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False

        if is_reddit_host(url):
            return False

        host = p.netloc.lower()
        for suf in BLOCKED_HOST_SUFFIXES:
            if host == suf or host.endswith("." + suf):
                return False

        path = (p.path or "").lower()
        for ext in BLOCKED_EXTENSIONS:
            if path.endswith(ext):
                return False

        return True
    except Exception:
        return False


# -----------------------
# Reddit RSS
# -----------------------

def fetch_subreddit_rss(subreddit: str):
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?ts={int(time.time())}"
    r = requests.get(url, headers=HEADERS_REDDIT, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.text).entries[:POST_LIMIT]


def extract_external_links(entry):
    links = set()
    summary = getattr(entry, "summary", "") or ""

    # Prefer href targets
    for href in HREF_RE.findall(summary):
        href = html.unescape(href)
        href = normalize_url(unwrap_reddit_media(href))
        if looks_like_news_or_blog_url(href):
            links.add(href)

    # Fallback
    if not links:
        for u in URL_RE_FALLBACK.findall(summary):
            u = html.unescape(u)
            u = normalize_url(unwrap_reddit_media(u))
            if looks_like_news_or_blog_url(u):
                links.add(u)

    return links


# -----------------------
# Title fetching and canonicalization
# -----------------------

def clean_title(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip(" \t\r\n-|")
    return t


def fetch_page_title_and_final_url(url: str):
    """
    Returns (title, final_url).
    If title looks like bot-block/paywall/consent garbage, returns (None, final_url).
    """
    final_url = url
    try:
        r = requests.get(url, headers=HEADERS_FETCH, timeout=18, allow_redirects=True)
        r.raise_for_status()

        final_url = normalize_url(r.url)

        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype and ("text/html" not in ctype):
            return None, final_url

        text = r.text
        if len(text) > 200_000:
            text = text[:200_000]

        title = None

        m = OG_TITLE_RE.search(text)
        if m:
            title = clean_title(m.group(1))

        if not title:
            m = TW_TITLE_RE.search(text)
            if m:
                title = clean_title(m.group(1))

        if not title:
            m = TITLE_TAG_RE.search(text)
            if m:
                title = clean_title(m.group(1))

        if not title:
            return None, final_url

        if len(title) < 12:
            return None, final_url

        if JUNK_TITLE_RE.search(title):
            return None, final_url

        return title, final_url
    except Exception:
        return None, final_url


def canonical_key(url: str) -> str:
    """
    Canonical URL key for deduping across all subreddits.
    Removes tracking params via normalize_url and strips trailing slash.
    """
    u = normalize_url(url)
    if u.endswith("/"):
        u = u[:-1]
    return u


# -----------------------
# RSS generation
# -----------------------

def build_rss(items):
    fg = FeedGenerator()
    fg.id("reddit:medicine:external-links")
    fg.title("External links from medicine subreddits")
    fg.description("Outbound articles and blog posts shared across selected medicine-related subreddits.")
    fg.link(href=FEED_URL, rel="self")
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
    # Deduped across ALL subreddits by canonical URL
    # canonical_url_key -> item dict
    items_by_url = {}

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

            for raw_link in outbound_links:
                # Fetch page title and final redirected URL
                article_title, final_url = fetch_page_title_and_final_url(raw_link)
                if not article_title:
                    continue

                key = canonical_key(final_url)

                # Build or merge an item for this URL
                if key not in items_by_url:
                    guid = hashlib.sha256(key.encode()).hexdigest()
                    items_by_url[key] = {
                        "guid": guid,
                        "title": article_title,
                        "link": final_url,
                        "published": published,
                        "subs": {sub},
                        "reddit_posts": [(sub, reddit_link, reddit_post_title)],
                    }
                else:
                    existing = items_by_url[key]
                    existing["subs"].add(sub)
                    existing["reddit_posts"].append((sub, reddit_link, reddit_post_title))

                    # Keep the most recent Reddit share date
                    if published > existing["published"]:
                        existing["published"] = published

                if len(items_by_url) >= MAX_ITEMS * 2:
                    # soft guardrail: avoid runaway work
                    break

            if len(items_by_url) >= MAX_ITEMS * 2:
                break

        if len(items_by_url) >= MAX_ITEMS * 2:
            break

    # Convert to list and build descriptions (include "also shared on")
    items = []
    for key, it in items_by_url.items():
        subs_sorted = sorted(it["subs"])
        shared_on = ", ".join([f"r/{s}" for s in subs_sorted])

        # Include up to 2 Reddit post references to keep descriptions readable
        post_bits = []
        for (s, link, title) in it["reddit_posts"][:2]:
            post_bits.append(f"r/{s}: {link}")

        description = f"Shared on {shared_on} | Reddit posts: " + " | ".join(post_bits)

        items.append({
            "guid": it["guid"],
            "title": it["title"],
            "link": it["link"],
            "published": it["published"],
            "description": description,
        })

    items.sort(key=lambda x: x["published"], reverse=True)
    items = items[:MAX_ITEMS]

    rss = build_rss(items)

    with open("rss.xml", "wb") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
