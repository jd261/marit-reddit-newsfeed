import re
import time
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from email.utils import parsedate_to_datetime

import requests
import feedparser
from feedgen.feed import FeedGenerator

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

POST_LIMIT = 50

HEADERS = {
    "User-Agent": "jd261-marit-reddit-newsfeed/0.1 (RSS fetch for personal use)"
}

URL_RE = re.compile(r"https?://\S+")

DROP_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "mc_cid", "mc_eid"
}

def normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip(').,]>"\'')
    try:
        p = urlparse(raw)
        if not p.scheme or not p.netloc:
            return raw

        q = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=True) if k not in DROP_QUERY_KEYS]
        new_query = urlencode(q)

        scheme = "https" if p.scheme in ("http", "https") else p.scheme
        return urlunparse((scheme, p.netloc, p.path, p.params, new_query, p.fragment))
    except Exception:
        return raw

def fetch_subreddit_rss(subreddit: str, limit: int = 50):
    # Cache-buster helps avoid stale responses
    url = f"https://www.reddit.com/r/{subreddit}/new.rss?ts={int(time.time())}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()

    feed = feedparser.parse(r.text)
    return feed.entries[:limit]

def extract_links_from_rss_entry(entry):
    links = set()

    # Link posts: entry.link is often the outbound destination
    if getattr(entry, "link", None):
        links.add(normalize_url(entry.link))

    # Some RSS entries include additional URLs in summary/content
    summary = getattr(entry, "summary", "") or ""
    for m in URL_RE.findall(summary):
        links.add(normalize_url(m))

    # Remove reddit internal links
    return {l for l in links if "reddit.com" not in l}

def build_rss(items):
    fg = FeedGenerator()
    fg.id("reddit:medicine:shared-links")
    fg.title("Reddit medicine links (RSS-based)")
    fg.description("Outbound links shared across selected medicine-related subreddits.")
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
        time.sleep(2.0)

        for e in entries:
            title = getattr(e, "title", "") or f"Link from r/{sub}"
            reddit_permalink = getattr(e, "link", "")

            published = datetime.now(timezone.utc)
            if getattr(e, "published", None):
                try:
                    published = parsedate_to_datetime(e.published).astimezone(timezone.utc)
                except Exception:
                    pass

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

    items.sort(key=lambda x: x["published"], reverse=True)
    rss_bytes = build_rss(items[:300])

    with open("rss.xml", "wb") as f:
        f.write(rss_bytes)

if __name__ == "__main__":
    main()
