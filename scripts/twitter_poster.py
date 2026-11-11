"""
Twitter/X Poster for WAYR - Autonomous social media posting using Twitter API v2.

This Modal function:
1. Checks WordPress RSS feed every 2 hours (20 minutes after pipeline runs)
2. Finds new articles that haven't been posted to Twitter yet
3. Posts them to Twitter using API v2
4. Tracks posted articles to avoid duplicates

Twitter API docs: https://developer.twitter.com/en/docs/twitter-api/tweets/manage-tweets/api-reference/post-tweets

Usage:
  # Deploy to production
  WAYR_ENV=prod modal deploy scripts/twitter_poster.py

  # Test manually
  modal run scripts/twitter_poster.py::post_to_twitter
"""

import os
import modal
from datetime import datetime

# Determine environment
ENVIRONMENT = os.getenv("WAYR_ENV", "prod")

# Create Modal app
app = modal.App(f"wayr-twitter-poster-{ENVIRONMENT}")

# Modal image with dependencies
image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "feedparser>=6.0.11",
    "requests>=2.31.0",
    "python-dotenv>=1.0.1",
    "requests-oauthlib>=1.3.1",  # For OAuth 1.0a
    "beautifulsoup4>=4.12.3",  # For parsing article content
)

# Secrets for Twitter credentials
secrets = [modal.Secret.from_name("wayr-secrets")]

# Persistent volume for tracking posted articles
volume = modal.Volume.from_name(
    "wayr-data-dev", create_if_missing=True  # Use same volume as main pipeline
)


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/data": volume},
    schedule=modal.Cron("20 */2 * * *"),  # Every 2 hours at :20 (20 min after pipeline)
    timeout=300,
)
def post_to_twitter():
    """
    Check WordPress RSS and post new articles to Twitter using API v2.

    Requires these environment variables in Modal secrets:
    - TWITTER_API_KEY: Your Twitter API key (Consumer Key)
    - TWITTER_API_SECRET: Your Twitter API secret (Consumer Secret)
    - TWITTER_ACCESS_TOKEN: Your access token
    - TWITTER_ACCESS_TOKEN_SECRET: Your access token secret
    - WORDPRESS_URL: Your WordPress site URL
    """
    import feedparser
    import json
    import html
    from pathlib import Path

    print("=" * 60)
    print("Starting Twitter poster...")
    print("=" * 60)

    # Load environment variables
    wordpress_url = os.environ.get("WORDPRESS_URL", "https://wayr.today")
    api_key = os.environ.get("TWITTER_API_KEY")
    api_secret = os.environ.get("TWITTER_API_SECRET")
    access_token = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_token_secret = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        print("ERROR: Twitter API credentials not set in Modal secrets")
        print(
            "Required: TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET"
        )
        return {"error": "Missing Twitter API credentials"}

    print(f"Twitter API credentials loaded")

    # Load tracking file
    tracking_file = Path("/data/twitter_posted.json")
    if tracking_file.exists():
        try:
            posted = json.loads(tracking_file.read_text())
            print(f"Loaded tracking file: {len(posted)} articles already posted")
        except Exception as e:
            print(f"Warning: Could not load tracking file: {e}")
            posted = {}
    else:
        print("No tracking file found, starting fresh")
        posted = {}

    # Fetch RSS feed
    rss_url = f"{wordpress_url.rstrip('/')}/feed/"
    print(f"\nFetching RSS feed: {rss_url}")

    try:
        feed = feedparser.parse(rss_url)
    except Exception as e:
        print(f"ERROR: Failed to fetch RSS feed: {e}")
        return {"error": str(e)}

    if not feed.entries:
        print("No articles found in RSS feed")
        return {"posted": 0, "message": "No articles in feed"}

    print(f"Found {len(feed.entries)} articles in RSS feed")

    # Check for new posts
    new_posts = 0
    daily_limit = 17  # Twitter Free tier limit: 17 tweets per day

    # Check how many we've posted in the last 24 hours
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    recent_posts = sum(
        1 for v in posted.values() if datetime.fromisoformat(v["posted_at"]) > day_ago
    )

    print(f"Posted in last 24h: {recent_posts}/{daily_limit}")

    if recent_posts >= daily_limit:
        print(f"WARNING: Daily limit reached ({daily_limit} posts/day)")
        return {
            "posted": 0,
            "message": f"Daily limit reached ({recent_posts}/{daily_limit})",
        }

    # Process articles (newest first, up to 10)
    for entry in feed.entries[:10]:
        article_url = entry.link

        if article_url in posted:
            print(f"  - Already posted: {entry.title[:50]}...")
            continue

        # Check if we've hit the daily limit
        if new_posts + recent_posts >= daily_limit:
            print(f"\n  Stopping: Would exceed daily limit ({daily_limit} posts/day)")
            break

        # Decode HTML entities (fixes &#8217; etc.)
        title = html.unescape(entry.title)

        print(f"\n  - Posting new article: {title[:50]}...")

        try:
            # Fetch article content to create a fresh summary
            import requests
            from bs4 import BeautifulSoup

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
                        if text and not text.startswith(
                            "This article was generated by AI"
                        ):
                            summary = text
                            break

            except Exception as e:
                print(f"    Warning: Could not fetch article content: {e}")
                # Fallback to RSS description if article fetch fails
                summary = html.unescape(entry.get("description", ""))
                if summary.startswith("This article was generated by AI"):
                    summary = summary.split(".", 1)[1].strip() if "." in summary else ""

            # Create tweet text (max 280 chars)
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

            # Post to Twitter
            result = post_tweet(
                api_key=api_key,
                api_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_token_secret,
                text=tweet_text,
            )

            if result.get("success"):
                # Mark as posted
                posted[article_url] = {
                    "posted_at": datetime.utcnow().isoformat(),
                    "title": title,
                    "tweet_id": result.get("tweet_id"),
                }
                new_posts += 1
                print(f"    ✓ Posted successfully (ID: {result.get('tweet_id')})")
            else:
                print(f"    ✗ Failed to post: {result.get('error')}")

        except Exception as e:
            print(f"    ✗ Error posting: {e}")
            continue

    # Save tracking file
    try:
        tracking_file.write_text(json.dumps(posted, indent=2))
        volume.commit()
        print(f"\n✓ Saved tracking file ({len(posted)} total articles tracked)")
    except Exception as e:
        print(f"\n✗ Warning: Could not save tracking file: {e}")

    print("\n" + "=" * 60)
    print(f"Twitter posting complete: {new_posts} new articles posted")
    print(f"Daily usage: {new_posts + recent_posts}/{daily_limit}")
    print("=" * 60)

    return {
        "posted": new_posts,
        "total_tracked": len(posted),
        "daily_usage": new_posts + recent_posts,
        "daily_limit": daily_limit,
        "message": f"Posted {new_posts} new articles",
    }


def post_tweet(
    api_key: str,
    api_secret: str,
    access_token: str,
    access_token_secret: str,
    text: str,
) -> dict:
    """
    Post a tweet using Twitter API v2 with OAuth 1.0a.

    API docs: https://developer.twitter.com/en/docs/twitter-api/tweets/manage-tweets/api-reference/post-tweets

    Args:
        api_key: Twitter API key (Consumer Key)
        api_secret: Twitter API secret (Consumer Secret)
        access_token: Access token
        access_token_secret: Access token secret
        text: Tweet text (max 280 characters)

    Returns:
        Dict with success status and tweet ID or error message
    """
    import requests
    from requests_oauthlib import OAuth1

    # Twitter API v2 endpoint
    api_url = "https://api.twitter.com/2/tweets"

    # OAuth 1.0a authentication
    auth = OAuth1(api_key, api_secret, access_token, access_token_secret)

    # Tweet payload
    payload = {"text": text}

    try:
        response = requests.post(api_url, auth=auth, json=payload, timeout=30)

        if response.status_code == 201:
            # Success
            response_data = response.json()
            tweet_id = response_data.get("data", {}).get("id", "unknown")
            return {"success": True, "tweet_id": tweet_id}
        else:
            # Error
            error_data = response.json() if response.text else {}
            error_msg = error_data.get("detail", error_data.get("title", response.text))
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {error_msg}",
                "response": error_data,
            }

    except Exception as e:
        return {"success": False, "error": str(e)}


# Local entrypoint for testing
@app.local_entrypoint()
def main():
    """Test the Twitter poster locally."""
    result = post_to_twitter.remote()
    print(f"\nResult: {result}")
