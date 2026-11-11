"""
One-time migration: merge non_news_cache.json into processed_articles.json.

Run before first deploy after consolidating NonNewsTracker into ArticleTracker.
Entries from non_news_cache.json are added with status="seen" and their
content_type preserved. Existing entries in processed_articles.json are not
overwritten.

Usage:
    python scripts/migrate_non_news_cache.py
"""

import json
from pathlib import Path

PROCESSED_FILE = Path("data/processed_articles.json")
NON_NEWS_FILE = Path("data/non_news_cache.json")


def migrate():
    if not NON_NEWS_FILE.exists():
        print("No non_news_cache.json found. Nothing to migrate.")
        return

    # Load both files
    processed = {}
    if PROCESSED_FILE.exists():
        with open(PROCESSED_FILE, "r") as f:
            processed = json.load(f)

    with open(NON_NEWS_FILE, "r") as f:
        non_news = json.load(f)

    print(f"Processed articles: {len(processed)}")
    print(f"Non-news articles:  {len(non_news)}")

    # Merge non-news entries that don't already exist in processed
    added = 0
    skipped = 0
    for url_hash, data in non_news.items():
        if not isinstance(data, dict):
            continue
        if url_hash in processed:
            # Entry already exists — just add content_type if missing
            existing = processed[url_hash]
            if isinstance(existing, dict) and not existing.get("content_type"):
                existing["content_type"] = data.get("content_type", "other")
                skipped += 1
            continue
        processed[url_hash] = {
            "timestamp": data.get("timestamp", ""),
            "title": data.get("title", ""),
            "url": data.get("url", ""),
            "status": "seen",
            "content_type": data.get("content_type", "other"),
        }
        added += 1

    # Save merged result
    with open(PROCESSED_FILE, "w") as f:
        json.dump(processed, f, indent=2)

    print(f"\nMigration complete:")
    print(f"  Added:   {added} non-news entries")
    print(f"  Updated: {skipped} existing entries (added content_type)")
    print(f"  Total:   {len(processed)} entries in processed_articles.json")
    print(f"\nYou can now safely delete {NON_NEWS_FILE}")


if __name__ == "__main__":
    migrate()
