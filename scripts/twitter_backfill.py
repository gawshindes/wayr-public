"""
Twitter Backfill Script - Post historical articles from WAYR to Twitter.

This script:
1. Fetches articles from WordPress RSS (last 20 days)
2. Posts them to Twitter with summaries
3. Respects Twitter rate limits (17 tweets/day max)
4. Tracks posted articles to avoid duplicates

Usage:
  python scripts/twitter_backfill.py --days 20 --max-posts 10
  python scripts/twitter_backfill.py --dry-run  # Preview without posting
"""

import os
import sys
import json
import html
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Add parent directory to path to import from scripts
sys.path.insert(0, str(Path(__file__).parent))

# Import the post_tweet function from twitter_poster
from twitter_poster import post_tweet


def main():
    """Backfill Twitter with historical articles."""
    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Backfill Twitter with historical WAYR articles"
    )
    parser.add_argument(
        "--days", type=int, default=20, help="Number of days to look back (default: 20)"
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=10,
        help="Maximum articles to post (default: 10)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without actually posting"
    )
    args = parser.parse_args()

    # Load .env file
    load_dotenv()

    print("=" * 60)
    print("Twitter Backfill - Post Historical Articles")
    print("=" * 60)
    print(f"Looking back: {args.days} days")
    print(f"Max posts: {args.max_posts}")
    print(f"Dry run: {args.dry_run}")

    # Load environment variables
    wordpress_url = os.environ.get("WORDPRESS_URL", "https://wayr.today")
    api_key = os.environ.get("TWITTER_API_KEY")
    api_secret = os.environ.get("TWITTER_API_SECRET")
    access_token = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_token_secret = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")

    # Validate credentials
    if not args.dry_run:
        if not all([api_key, api_secret, access_token, access_token_secret]):
            print("\n❌ ERROR: Twitter API credentials not set in .env file")
            print("\nRequired environment variables:")
            print("  - TWITTER_API_KEY")
            print("  - TWITTER_API_SECRET")
            print("  - TWITTER_ACCESS_TOKEN")
            print("  - TWITTER_ACCESS_TOKEN_SECRET")
            print("\nPlease add these to your .env file and try again.")
            sys.exit(1)

        print(f"\n✓ Twitter API credentials loaded from .env")

    print(f"✓ WordPress URL: {wordpress_url}")

    # Use production tracking file
    tracking_file = Path("data/twitter_posted.json")
    tracking_file.parent.mkdir(exist_ok=True)

    if tracking_file.exists():
        try:
            posted = json.loads(tracking_file.read_text())
            print(f"✓ Loaded tracking file: {len(posted)} articles already posted")
        except Exception as e:
            print(f"⚠️  Warning: Could not load tracking file: {e}")
            posted = {}
    else:
        print("✓ No tracking file found, starting fresh")
        posted = {}

    # Fetch articles from WordPress REST API (supports more results than RSS)
    print(f"\n{'='*60}")
    print("Fetching articles from WordPress REST API...")
    print("=" * 60)

    import requests

    # Calculate date range
    cutoff_date = datetime.utcnow() - timedelta(days=args.days)
    cutoff_iso = cutoff_date.strftime("%Y-%m-%dT%H:%M:%S")

    # WordPress REST API endpoint
    api_url = f"{wordpress_url.rstrip('/')}/wp-json/wp/v2/posts"

    print(f"URL: {api_url}")
    print(f"Filtering articles published after {cutoff_date.strftime('%Y-%m-%d')}")

    # Fetch all posts with pagination
    all_posts = []
    page = 1
    per_page = 100

    while True:
        params = {
            "per_page": per_page,
            "page": page,
            "after": cutoff_iso,  # Filter by date
            "orderby": "date",
            "order": "asc",  # Oldest first (chronological order)
        }

        try:
            response = requests.get(api_url, params=params, timeout=30)
            response.raise_for_status()
            posts = response.json()

            if not posts:
                break

            all_posts.extend(posts)
            print(
                f"  Fetched page {page}: {len(posts)} articles (total: {len(all_posts)})"
            )

            # Check if there are more pages
            total_pages = int(response.headers.get("X-WP-TotalPages", 1))
            if page >= total_pages:
                break

            page += 1

        except Exception as e:
            print(f"\n❌ ERROR: Failed to fetch articles from WordPress API: {e}")
            sys.exit(1)

    posts = all_posts

    if not posts:
        print("\n⚠️  No articles found in WordPress")
        sys.exit(0)

    print(f"✓ Found {len(posts)} articles from last {args.days} days")

    # Convert WordPress posts to format compatible with rest of script
    recent_articles = []
    for post in posts:
        # Create entry-like object
        class Entry:
            def __init__(self, post_data):
                self.link = post_data["link"]
                self.title = html.unescape(post_data["title"]["rendered"])
                # Extract plain text from excerpt
                excerpt_html = post_data.get("excerpt", {}).get("rendered", "")
                from bs4 import BeautifulSoup

                excerpt_soup = BeautifulSoup(excerpt_html, "html.parser")
                self.description = excerpt_soup.get_text().strip()

            def get(self, key, default=""):
                return getattr(self, key, default)

        entry = Entry(post)
        pub_date = datetime.strptime(post["date"], "%Y-%m-%dT%H:%M:%S")
        recent_articles.append((entry, pub_date))

    print(f"✓ Prepared {len(recent_articles)} articles for posting")

    # Sort by date (oldest first, so they appear in chronological order)
    recent_articles.sort(key=lambda x: x[1])

    # Filter out already posted articles
    new_articles = [
        (entry, pub_date)
        for entry, pub_date in recent_articles
        if entry.link not in posted
    ]

    print(f"✓ Found {len(new_articles)} new articles to post")

    if not new_articles:
        print("\n✅ No new articles to post!")
        sys.exit(0)

    # Limit to max_posts
    articles_to_post = new_articles[: args.max_posts]
    print(
        f"\n✓ Will post {len(articles_to_post)} articles (limited to {args.max_posts})"
    )

    # Post articles
    print(f"\n{'='*60}")
    print(f"{'DRY RUN - ' if args.dry_run else ''}Posting articles to Twitter...")
    print("=" * 60)

    import requests
    from bs4 import BeautifulSoup

    posted_count = 0
    for i, (entry, pub_date) in enumerate(articles_to_post, 1):
        article_url = entry.link
        title = html.unescape(entry.title)

        print(f"\n[{i}/{len(articles_to_post)}] {title[:70]}...")
        print(f"  Published: {pub_date.strftime('%Y-%m-%d %H:%M')}")
        print(f"  URL: {article_url}")

        # Fetch article content to create a fresh summary
        summary = ""
        try:
            # Fetch the article page
            response = requests.get(article_url, timeout=10)
            response.raise_for_status()

            # Parse HTML and extract article content
            soup = BeautifulSoup(response.text, "html.parser")

            # Try to find article content (common selectors for WordPress)
            article_content = None
            for selector in [
                "article .entry-content",
                "article",
                ".post-content",
                ".content",
            ]:
                article_content = soup.select_one(selector)
                if article_content:
                    break

            if article_content:
                # Get first paragraph that's not the AI disclosure
                paragraphs = article_content.find_all("p")
                for p in paragraphs:
                    text = p.get_text().strip()
                    # Skip AI disclosure and empty paragraphs
                    if text and not text.startswith("This article was generated by AI"):
                        summary = text
                        break

            print(f"  ✓ Extracted summary from article")

        except Exception as e:
            print(f"  ⚠️  Warning: Could not fetch article content: {e}")
            # Fallback to RSS description if article fetch fails
            summary = html.unescape(entry.get("description", ""))
            if summary.startswith("This article was generated by AI"):
                summary = summary.split(".", 1)[1].strip() if "." in summary else ""
            print(f"  ✓ Using RSS description as fallback")

        # Create tweet text (same logic as twitter_poster.py)
        # Twitter auto-shortens URLs to 23 chars (t.co links)
        url_length = 23
        max_chars = 280

        # Calculate available space for title + summary
        # Account for: title + "\n\n" (2 chars) + "\n\n" (2 chars) + URL (23 chars)
        available_for_text = max_chars - url_length - 4  # 253 chars

        # Create tweet based on available space
        if len(title) <= available_for_text - 30:  # Leave room for summary
            # Title fits, add summary
            remaining = available_for_text - len(title)
            if remaining > 30 and summary:
                # Truncate summary to fit, break at word boundary
                summary_short = summary[: remaining - 3].rsplit(" ", 1)[0] + "..."
                tweet_text = f"{title}\n\n{summary_short}"
            else:
                # Not enough room for summary, just title
                tweet_text = title
        else:
            # Title is too long, truncate at word boundary
            tweet_text = title[: available_for_text - 3].rsplit(" ", 1)[0] + "..."

        # Add URL on a new line
        tweet_text = f"{tweet_text}\n\n{article_url}"

        print(f"  Tweet length: {len(tweet_text)} characters")

        if args.dry_run:
            print(f"  🔍 DRY RUN - Would post this tweet:")
            print(f"     {tweet_text[:100]}...")
            posted_count += 1
            continue

        # Post to Twitter
        try:
            result = post_tweet(
                api_key=api_key,
                api_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_token_secret,
                text=tweet_text,
            )

            if result.get("success"):
                print(f"  ✅ Posted successfully!")
                print(f"     Tweet ID: {result.get('tweet_id')}")

                # Mark as posted
                posted[article_url] = {
                    "posted_at": datetime.utcnow().isoformat(),
                    "title": title,
                    "tweet_id": result.get("tweet_id"),
                }
                posted_count += 1

                # Save tracking file after each post
                tracking_file.write_text(json.dumps(posted, indent=2))

            else:
                print(f"  ❌ Failed to post: {result.get('error')}")
                if result.get("response"):
                    print(f"     API Response: {result.get('response')}")

        except Exception as e:
            print(f"  ❌ Error posting to Twitter: {e}")
            continue

    # Save tracking file
    if not args.dry_run:
        try:
            tracking_file.write_text(json.dumps(posted, indent=2))
            print(f"\n✓ Saved tracking file ({len(posted)} total articles tracked)")
        except Exception as e:
            print(f"\n✗ Warning: Could not save tracking file: {e}")

    print("\n" + "=" * 60)
    print(f"✅ Backfill complete!")
    print(f"   Posted: {posted_count}/{len(articles_to_post)} articles")
    if args.dry_run:
        print(f"   (DRY RUN - no actual posts made)")
    print("=" * 60)


if __name__ == "__main__":
    main()
