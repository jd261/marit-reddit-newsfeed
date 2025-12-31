import re
import time
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from feedgen.feed import FeedGenerator

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
    "User-Agent": "reddit-links-to-rss/0.1 (by u/yourusername; contact: youremail@example.com)"
}

URL_RE = re.compile(r'https?://\S+')

# Tracking params to drop. Keep this list short and practical.
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

def extract_outbound_links(post_data: dict):
    links = set()

    # Link posts
    dest = post_data.get("url_overridden_by_dest") or post_data.get("url")
    if dest and "reddit.com" not in dest:
        links.add(normalize_url(dest))

    # Text posts with pasted links
    selftext = post_data.get("selftext") or ""
    for m in URL_RE.findall(selftext):
        m = normalize_url(m)
        if "reddit.com" not in m:
            links.add(m)

    return links

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
        posts = fetch_new_posts(sub, POST_LIMIT)
        time.sleep(1.0)  # polite pacing

        for p in posts:
            d = p["data"]
            created = datetime.fromtimestamp(d["created_utc"], tz=timezone.utc)
            reddit_permalink = "https://www.reddit.com" + (d.get("permalink") or "")

            for link in extract_outbound_links(d):
                # Use a stable GUID so RSS readers do not re-import the same item
                guid = hashlib.sha256(f"{sub}|{d.get('id')}|{link}".encode()).hexdigest()
                if guid in seen:
                    continue
                seen.add(guid)

                items.append({
                    "guid": guid,
                    "title": d.get("title") or f"Link from r/{sub}",
                    "link": link,
                    "published": created,
                    "description": f"Source: r/{sub} | score {d.get('score')} | comments {d.get('num_comments')} | Reddit: {reddit_permalink}"
                })

    # Keep the feed to a reasonable size
    items.sort(key=lambda x: x["published"], reverse=True)
    rss_bytes = build_rss(items[:300])

    with open("rss.xml", "wb") as f:
        f.write(rss_bytes)

if __name__ == "__main__":
    main()
