"""
Rebuild article cache from published WordPress posts.

This script:
1. Fetches all published posts from WordPress (last 30 days)
2. Extracts source URLs from article content
3. Builds cache JSON matching ArticleTracker format
4. Saves to cache_rebuild.json for upload to Modal

Usage:
    python rebuild_cache.py
"""

import os
import re
import json
import hashlib
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth

# Load environment variables
load_dotenv(dotenv_path=".env", override=True)

# WordPress credentials
WORDPRESS_URL = os.getenv("WORDPRESS_URL")
WORDPRESS_USERNAME = os.getenv("WORDPRESS_USERNAME")
WORDPRESS_APP_PASSWORD = os.getenv("WORDPRESS_APP_PASSWORD")


def get_article_hash(url: str) -> str:
    """
    Generate URL hash using same method as ArticleTracker.

    Args:
        url: Article source URL

    Returns:
        16-character hash
    """
    url_normalized = url.lower().strip()
    return hashlib.sha256(url_normalized.encode()).hexdigest()[:16]


def extract_source_url_from_content(content: str) -> str:
    """
    Extract original source URL from article HTML content.

    Args:
        content: HTML content of WordPress post

    Returns:
        Source URL or empty string if not found
    """
    # Parse HTML
    soup = BeautifulSoup(content, "html.parser")

    # Look for link with text like "TechCrunch", "The Verge", etc. or "here"
    # These are typically at the end like: "For more details, check out <a href='...'>here</a>"
    links = soup.find_all("a", href=True)

    for link in links:
        href = link.get("href", "")
        # Skip internal WordPress links
        if "wayr.today" in href or "whitesmoke-camel" in href:
            continue
        # Look for common tech news domains
        if any(
            domain in href
            for domain in [
                "techcrunch.com",
                "theverge.com",
                "arstechnica.com",
                "wired.com",
                "venturebeat.com",
                "engadget.com",
                "cnet.com",
                "zdnet.com",
                "mashable.com",
                "recode.net",
            ]
        ):
            return href

    return ""


def fetch_wordpress_posts(days: int = 30) -> list:
    """
    Fetch all published WordPress posts from last N days.

    Args:
        days: Number of days to look back

    Returns:
        List of WordPress post objects
    """
    auth = HTTPBasicAuth(WORDPRESS_USERNAME, WORDPRESS_APP_PASSWORD)
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    cutoff_str = cutoff_date.strftime("%Y-%m-%dT%H:%M:%S")

    all_posts = []
    page = 1

    print(f"Fetching WordPress posts from {WORDPRESS_URL}...")
    print(f"Looking for posts after: {cutoff_str}")

    while True:
        url = f"{WORDPRESS_URL.rstrip('/')}/wp-json/wp/v2/posts"
        params = {
            "per_page": 100,
            "page": page,
            "status": "publish",
            "after": cutoff_str,
            "orderby": "date",
            "order": "desc",
        }

        try:
            response = requests.get(url, params=params, auth=auth, timeout=30)
            response.raise_for_status()

            posts = response.json()

            if not posts:
                break

            all_posts.extend(posts)
            print(f"  Page {page}: Fetched {len(posts)} posts")

            # Check if there are more pages
            total_pages = int(response.headers.get("X-WP-TotalPages", 1))
            if page >= total_pages:
                break

            page += 1

        except Exception as e:
            print(f"Error fetching posts: {e}")
            break

    print(f"Total posts fetched: {len(all_posts)}")
    return all_posts


def build_cache_from_posts(posts: list) -> dict:
    """
    Build article cache from WordPress posts.

    Args:
        posts: List of WordPress post objects

    Returns:
        Cache dictionary {url_hash: {timestamp, title}}
    """
    cache = {}
    successful = 0
    failed = 0

    print("\nBuilding cache from posts...")

    for post in posts:
        try:
            # Extract post data
            title = post.get("title", {}).get("rendered", "")
            content = post.get("content", {}).get("rendered", "")
            date = post.get("date", "")

            # Clean HTML entities from title
            from html import unescape

            title = unescape(title)

            # Extract source URL from content
            source_url = extract_source_url_from_content(content)

            if not source_url:
                print(f"  ⚠ No source URL found for: {title[:50]}...")
                failed += 1
                continue

            # Generate hash
            url_hash = get_article_hash(source_url)

            # Add to cache (new shape — see ArticleTracker)
            cache[url_hash] = {
                "created_at": date,
                "updated_at": date,
                "title": title,
                "url": source_url,
                "source": "",
                "last_stage": "published",
                "industry": None,
                "news_type": None,
                "category": None,
            }

            successful += 1
            print(f"  ✓ {title[:60]}...")

        except Exception as e:
            print(f"  ✗ Error processing post: {e}")
            failed += 1
            continue

    print(f"\nCache build complete:")
    print(f"  Successful: {successful}")
    print(f"  Failed: {failed}")
    print(f"  Total cache entries: {len(cache)}")

    return cache


def main():
    """Main function."""
    print("=" * 60)
    print("Article Cache Rebuild Script")
    print("=" * 60)
    print(f"WordPress URL: {WORDPRESS_URL}")
    print()

    # Fetch posts
    posts = fetch_wordpress_posts(days=30)

    if not posts:
        print("No posts found. Exiting.")
        return

    # Build cache
    cache = build_cache_from_posts(posts)

    # Save to file
    output_file = "cache_rebuild.json"
    with open(output_file, "w") as f:
        json.dump(cache, f, indent=2)

    print()
    print("=" * 60)
    print(f"Cache saved to: {output_file}")
    print("=" * 60)
    print()
    print("Next steps:")
    print("1. Review cache_rebuild.json to verify entries")
    print("2. Run: python upload_cache_to_modal.py")
    print()


if __name__ == "__main__":
    main()
