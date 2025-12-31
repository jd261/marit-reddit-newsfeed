import re
import time
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, unquote
from email.utils import parsedate_to_datetime

import requests
import feedparser
from feedgen.feed import FeedGenerator


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

HEADERS = {
    "User-Agent": "jd261-marit-reddit-newsfeed/0.3 (RSS aggregation)"
}

URL_RE = re.compile(r"https?://\S+")

DROP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid",
}

REDDIT_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "redd.it",
    "i.redd.it",
    "v.redd.it",
    "preview.redd.it",
}


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
    try:
        p = urlparse(url)
        if p.netloc.endswith("reddit.com") and p.path == "/media":
            qs = dict(parse_qsl(p.query))
            if "url" in qs:
                return normalize_url(unquote(qs["url"]))
    except Exception:
        pass
    return url


def fetch_subreddit_rss(subreddit: str):
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?ts={int(time.time())}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.text).entries[:POST_LIMIT]


def extract_external_links(entry):
    links = set()

    text = getattr(entry, "summary", "") or ""
    for m in URL_RE.findall(text):
        links.add(normalize_url(m))

    if getattr(entry, "link", None):
        links.add(normalize_url(entry.link))

    cleaned = set()
    for l in links:
        l = unwrap_reddit_media(l)
        if not is_reddit_host(l):
            cleaned.add(l)

    return cleaned


def build_rss(items):
    fg = FeedGenerator()
    fg.id("reddit:medicine:external-links")
    fg.title("External links from medicine subreddits")
    fg.description("Outbound articles shared across medicine-related subreddits.")
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
            title = getattr(e, "title", f"Link from r/{sub}")
            reddit_link = getattr(e, "link", "")

            try:
                published = parsedate_to_datetime(e.published).astimezone(timezone.utc)
            except Exception:
                published = datetime.now(timezone.utc)

            for link in extract_external_links(e):
                guid = hashlib.sha256(f"{sub}|{title}|{link}".encode()).hexdigest()
                if guid in seen:
                    continue

                seen.add(guid)
                items.append({
                    "guid": guid,
                    "title": title,
                    "link": link,
                    "published": published,
                    "description": f"Source: r/{sub} | Reddit: {reddit_link}",
                })

    items.sort(key=lambda x: x["published"], reverse=True)
    rss = build_rss(items[:300])

    with open("rss.xml", "wb") as f:
        f.write(rss)


if __name__ == "__main__":
    main()
