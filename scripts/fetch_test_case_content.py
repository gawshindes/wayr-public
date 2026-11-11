#!/usr/bin/env python3
"""Script to fetch RSS content for test cases from production articles."""

import csv
import json
import asyncio
from pathlib import Path
from wayr.services.rss_fetcher import RSSFetcher
from wayr.config import Settings


async def fetch_article_content(url: str) -> str:
    """Fetch content from RSS feeds for a specific URL."""
    # Get RSS feeds from settings
    settings = Settings()

    # Create RSS fetcher
    fetcher = RSSFetcher(settings.rss_feeds)

    # Fetch all articles
    articles = await fetcher.fetch_all()

    # Find matching article by URL
    for article in articles:
        if str(article.url) == url or url in str(article.url):
            return article.content or article.summary

    return ""


async def main():
    """Main function to process CSV and fetch content."""
    csv_file = Path("uploaded/articals.csv")
    test_cases_file = Path("wayr/evaluation/classification/test_cases.json")

    # Read CSV
    articles_to_fetch = []
    with open(csv_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            comment = row.get("Coment", "").strip()
            if comment and comment != "Duplicate":  # Only non-duplicates with comments
                articles_to_fetch.append(
                    {
                        "title": row["wp-block-post-title"],
                        "url": row["wp-block-post-title href"],
                        "category": row["taxonomy-category"],
                        "comment": comment,
                    }
                )

    print(f"Found {len(articles_to_fetch)} articles to fetch content for")

    # Fetch content for each
    for article in articles_to_fetch[:10]:  # Limit to first 10 for now
        print(f"\nFetching: {article['title']}")
        content = await fetch_article_content(article["url"])
        if content:
            print(f"  ✓ Found content ({len(content)} chars)")
            article["content"] = content
        else:
            print(f"  ✗ No content found")

    # Save results
    output_file = Path("uploaded/test_case_content.json")
    with open(output_file, "w") as f:
        json.dump(articles_to_fetch, f, indent=2)

    print(f"\n✓ Saved results to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
