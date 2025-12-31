import re
import time
import hashlib
from datetime import datetime, timezone
from urllib.parse import (
    urlparse,
    urlunparse,
    parse_qsl,
    urlencode,
    unquote,
)
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

HEADERS = {
    "User-Agent": "jd261-marit-reddit-newsfeed/0.2 (RSS aggregation for personal use)"
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


# -----------------------
# Helpers
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
        new_query = urlencode(q)

        scheme = "https" if p.scheme in ("http", "https") else p.scheme
        return urlunparse(
            (scheme, p.netloc, p.path, p.params, new_query, p.fragment)
        )
    except Exception:
        return raw


def is_reddit_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host in REDDIT_HOSTS or host.endswith(".reddit.com")
    except Exception:
        return False


def unwrap_reddit_media(url: str) -> str:
    """
    Converts:
      https://www.reddit.com/media?url=<ENCODED_URL>
    into the real outbound URL.
    """
    try:
        p = urlparse(url)
        if (
            p.netloc.lower() in {"reddit.com", "www.reddit.com", "old.reddit.com"}
            and p.path == "/media"
        ):
            qs = dict(parse_qsl(p.query))
            if "url" in qs and qs["url"]:
                return normalize_url(unquote(qs["url"]))
    except Exception:
        pass

    return url


def fetch_subreddit_rss(subreddit: str, limit: int):
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?ts={int(time.time())}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()

    feed = feedparser.parse(r.text)
    return feed.entries[:limit]


def extract_external_links(entry):
    links = set()

    summary = getattr(entry, "summary", "") or ""
    for m in URL_RE.findall(summary):
        links.add(normalize_url(m))

    if getattr(entry, "link", None):
        links.add(normalize_url(entry.link))

    cleaned = set()
    for l in links:
        l = unwrap_reddit_media(l)
        if is_reddit_host(l):
            continue
        cleaned.add(l)

    return cleaned


# -----------------------
# RSS generation
# -----------------------

def build_rss(items):
    fg = FeedGenerator()
    fg.id("reddit:medicine:external-links")
    fg.title("External links shared across medicine subreddits")
    fg.description(
        "Outbound articles and resources shared across selected medicine-related subreddits."
    )
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


# -----------------------
# Main
# -----------------------

def main():
    seen = set()
    items = []

    for sub in SUBREDDITS:
        entries = fetch_subreddit_rss(sub, POST_LIMIT)
        time.sleep(2.0)

        for e in entries:
            title = getattr(e, "title", "") or f"Link from r/{sub}"
            reddit_link = getattr(e, "link", "")

            published = datetime.now(timezone.utc)
            if getattr(e, "published", None):
