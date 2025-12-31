import re
import time
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from feedgen.feed import FeedGenerator

import feedparser
from email.utils import parsedate_to_datetime

# 1) Start with a small list, expand later
SUBREDDITS = [
    "medicine",
    "emergencymedicine",
    "FamilyMedicine",
    "Psychiatry",
    "Anesthesiology",
    "Radiology",
    "InternalMedicine",
    "criticalcare",
    "neurology",
    "ophthalmology",
]

# How many posts per subreddit per run
POST_LIMIT = 75

# User-Agent matters for Reddit requests
HEADERS = {
    "User-Agent": "jd261-marit-reddit-newsfeed/0.1 (RSS fetch for personal use)"
}

URL_RE = re.compile(r'https?://\S+')

# Tracking params to drop. Keep this list short and practical.
DROP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid"
}

def fetch_subreddit_rss(subreddit: str, limit: int = 50):
    # Reddit RSS endpoint for newest posts
    url = f"https://www.reddit.com/r/{subreddit}/new.rss"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()

    feed = feedparser.parse(r.text)
    return feed.entries[:limit]

def normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip(').,]>"\'')
    try:
        p = urlparse(raw)
        if not p.scheme or not p.netloc:
            return raw

        # Remove common tracking query params
        q = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=True) if k not in DROP_QUERY_KEYS]
        new_query = urlencode(q)

        # Normalize scheme to https when possible
        scheme = "https" if p.scheme in ("http", "https") else p.scheme

        return urlunparse((scheme, p.netloc, p.path, p.params, new_query, p.fragment))
    except Exception:
        return raw

def fetch_new_posts(subreddit: str, limit: int):
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()["data"]["children"]

def extract_links_from_rss_entry(entry):
    links = set()

    # For link posts, entry.link is usually the outbound destination
    if hasattr(entry, "link") and entry.link:
        links.add(normalize_url(entry.link))

    # Also scan the summary/content for additional URLs when present
    summary = getattr(entry, "summary", "") or ""
    for m in URL_RE.findall(summary):
        links.add(normalize_url(m))

    return {l for l in links if "reddit.com" not in l}

def build_rss(items):
    fg = FeedGenerator()
    fg.id("reddit:medicine:shared-links")
    fg.title("Reddit medicine links (shared across selected subreddits)")
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
    entries = fetch_subreddit_rss(sub, POST_LIMIT)
    time.sleep(1.0)

    for e in entries:
        published = datetime.now(timezone.utc)
        if getattr(e, "published", None):
            try:
                published = parsedate_to_datetime(e.published).astimezone(timezone.utc)
            except Exception:
                pass

        reddit_permalink = getattr(e, "link", "")

        title = getattr(e, "title", "") or f"Link from r/{sub}"

        for link in extract_links_from_rss_entry(e):
            guid = hashlib.sha256(f"{sub}|{title}|{link}".encode()).hexdigest()
            if guid in seen:
                continue
            seen.add(guid)

            items.append({
                "guid": guid,
                "title": title,
                "link": link,
                "published": published,
                "description": f"Source: r/{sub} | Reddit: {reddit_permalink}"
            })

    # Keep the feed to a reasonable size
    items.sort(key=lambda x: x["published"], reverse=True)
    rss_bytes = build_rss(items[:300])

    with open("rss.xml", "wb") as f:
        f.write(rss_bytes)

if __name__ == "__main__":
    main()
