"""
Cleanup script to delete problematic WordPress posts.

This script deletes:
1. Deal/promotional articles that should have been filtered
2. Duplicate articles (same story from different sources)
3. Default WordPress "Hello world!" post

Usage:
    python cleanup_wordpress.py
"""

import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from typing import List

# Load environment variables from .env file explicitly (not .env.testing)
load_dotenv(dotenv_path=".env", override=True)

# WordPress credentials
WORDPRESS_URL = os.getenv("WORDPRESS_URL")
WORDPRESS_USERNAME = os.getenv("WORDPRESS_USERNAME")
WORDPRESS_APP_PASSWORD = os.getenv("WORDPRESS_APP_PASSWORD")

# Debug: Print the URL being used
print(f"DEBUG: Using WordPress URL from .env: {WORDPRESS_URL}")

# Posts to delete (identified from XML export)
POSTS_TO_DELETE = {
    # Default WordPress post
    1: "Hello world!",
    # Duplicate South Korea Google Maps articles (keep latest, delete older ones)
    6: "South Korea Contemplates Granting Google and Apple Access... (duplicate 1)",
    8: "South Korea Weighs Granting Google and Apple Access... (duplicate 2)",
    # Deal/promotional articles (should have been filtered)
    7: "Score the Best Deals on Apple AirPods",
    9: "Uncover the Best Deals on Apple AirPods: A Tech Enthusiast's Insider Guide",
    11: "Uncover the Best Deals on Apple's AirPods: Your Guide to Scoring Discounts",
    14: "Apple Unveils Powerful M5 MacBook Pro and iPad Pro: Preorder Now",
}


def trash_post(post_id: int, title: str, max_retries: int = 3) -> bool:
    """
    Move a WordPress post to trash by ID with retry logic.

    Args:
        post_id: WordPress post ID
        title: Post title (for logging)
        max_retries: Maximum number of retry attempts

    Returns:
        True if successful, False otherwise
    """
    url = f"{WORDPRESS_URL.rstrip('/')}/wp-json/wp/v2/posts/{post_id}"

    # Use HTTPBasicAuth like WordPressClient does
    auth = HTTPBasicAuth(WORDPRESS_USERNAME, WORDPRESS_APP_PASSWORD)

    for attempt in range(max_retries):
        try:
            # Use DELETE without force=True to move to trash (not permanent delete)
            # WordPress API: DELETE /wp/v2/posts/{id} moves to trash
            # DELETE /wp/v2/posts/{id}?force=true permanently deletes
            response = requests.delete(url, auth=auth, timeout=30)

            if response.status_code == 200:
                print(f"✓ Moved to trash post {post_id}: {title}")
                return True
            elif response.status_code == 404:
                print(f"⚠ Post {post_id} not found (may already be deleted): {title}")
                return True  # Consider this a success
            elif response.status_code == 401:
                print(f"✗ Authentication failed for post {post_id}: {title}")
                print(f"  Check WordPress credentials")
                return False
            else:
                print(f"✗ Failed to trash post {post_id}: {title}")
                print(f"  Status: {response.status_code}")
                print(f"  Response: {response.text[:200]}")
                return False

        except requests.exceptions.ConnectionError as e:
            if attempt < max_retries - 1:
                print(
                    f"⚠ Connection error for post {post_id}, retrying... (attempt {attempt + 1}/{max_retries})"
                )
                import time

                time.sleep(2)  # Wait before retry
            else:
                print(
                    f"✗ Connection error after {max_retries} attempts for post {post_id}: {title}"
                )
                print(f"  Error: {e}")
                return False

        except Exception as e:
            print(f"✗ Error trashing post {post_id}: {title}")
            print(f"  Error: {e}")
            return False

    return False


def main():
    """Main cleanup function."""
    import sys

    print("=" * 60)
    print("WordPress Cleanup Script")
    print("=" * 60)
    print(f"WordPress URL: {WORDPRESS_URL}")
    print(f"Posts to delete: {len(POSTS_TO_DELETE)}")
    print()

    # Confirm before proceeding
    print("Posts scheduled for deletion:")
    for post_id, title in POSTS_TO_DELETE.items():
        print(f"  - ID {post_id}: {title}")
    print()

    # Check for --yes flag to skip confirmation
    if "--yes" not in sys.argv:
        try:
            confirm = input("Proceed with deletion? (yes/no): ")
            if confirm.lower() != "yes":
                print("Aborted.")
                return
        except EOFError:
            print("\nNo input detected. Use --yes flag to auto-confirm.")
            return

    print()
    print("Moving posts to trash...")
    print("-" * 60)

    # Trash posts
    success_count = 0
    failed_count = 0

    for post_id, title in POSTS_TO_DELETE.items():
        if trash_post(post_id, title):
            success_count += 1
        else:
            failed_count += 1

    # Summary
    print()
    print("=" * 60)
    print("Cleanup Complete")
    print("=" * 60)
    print(f"Successfully moved to trash: {success_count}")
    print(f"Failed: {failed_count}")
    print()

    if failed_count == 0:
        print("✓ All problematic posts have been moved to trash!")
        print("  You can permanently delete them from WordPress admin > Posts > Trash")
    else:
        print("⚠ Some posts could not be trashed. Check errors above.")


if __name__ == "__main__":
    main()
