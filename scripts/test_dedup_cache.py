"""
Test LLM-based dedup against a processed_articles.json cache.
Finds all duplicate groups by processing titles in batches of 50.

Usage:
    poetry run python scripts/test_dedup_cache.py [path_to_cache.json] [env_file]
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

env_file = sys.argv[2] if len(sys.argv) > 2 else ".env-india"
load_dotenv(env_file)

from wayr.agents.deduplication import DeduplicationAgent

BATCH_SIZE = 50


async def main():
    cache_file = sys.argv[1] if len(sys.argv) > 1 else "data/processed_articles.json"
    if not os.path.exists(cache_file):
        print(f"Cache file not found: {cache_file}")
        sys.exit(1)

    with open(cache_file) as f:
        cache = json.load(f)

    # Extract all titles
    all_titles = []
    for hash_id, data in cache.items():
        if isinstance(data, dict) and data.get("title"):
            all_titles.append(
                {
                    "hash": hash_id,
                    "title": data["title"],
                    "timestamp": data.get("timestamp", ""),
                }
            )

    print(f"Loaded {len(all_titles)} articles from {cache_file}")
    print(f"Processing in batches of {BATCH_SIZE}...\n")

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY not set")
        sys.exit(1)

    agent = DeduplicationAgent(
        api_key=api_key,
        model="gemini-flash-latest",
        provider="google",
    )

    all_groups = []
    total_removed = 0

    # Process in batches — each batch compares within itself
    # AND against the previous batch (sliding window for cross-batch detection)
    for batch_start in range(0, len(all_titles), BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, len(all_titles))
        batch = all_titles[batch_start:batch_end]

        batch_titles = [
            {"index": i, "title": item["title"]} for i, item in enumerate(batch)
        ]

        # Include previous batch as "cached" for cross-batch detection
        cached_titles = None
        if batch_start > 0:
            prev_start = max(0, batch_start - BATCH_SIZE)
            prev_batch = all_titles[prev_start:batch_start]
            offset = len(batch)
            cached_titles = [
                {"index": offset + i, "title": item["title"]}
                for i, item in enumerate(prev_batch)
            ]

        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(all_titles) + BATCH_SIZE - 1) // BATCH_SIZE
        print(
            f"[Batch {batch_num}/{total_batches}] Processing articles {batch_start+1}-{batch_end}..."
        )

        result = await agent.find_duplicates(
            batch_titles=batch_titles,
            cached_titles=cached_titles,
        )

        groups = result["duplicate_groups"]
        to_remove = result["batch_duplicates_to_remove"]

        if groups:
            for group in groups:
                # Map indices back to actual titles
                title_map = {}
                for i, item in enumerate(batch):
                    title_map[i] = item
                if cached_titles:
                    prev_start_idx = max(0, batch_start - BATCH_SIZE)
                    prev_batch = all_titles[prev_start_idx:batch_start]
                    offset = len(batch)
                    for i, item in enumerate(prev_batch):
                        title_map[offset + i] = item

                group_info = []
                for idx in group:
                    item = title_map.get(idx, {"title": "?", "timestamp": ""})
                    kept = "KEEP" if idx not in to_remove else "REMOVE"
                    group_info.append((kept, item))

                all_groups.append(group_info)
                total_removed += len(to_remove)

                print(f"  Found duplicate group:")
                for kept, item in group_info:
                    print(f"    [{kept}] [{item['timestamp'][:10]}] {item['title']}")

        # Small delay to avoid rate limiting
        await asyncio.sleep(0.5)

    print(f"\n{'='*80}")
    print(f"RESULTS: Found {len(all_groups)} duplicate group(s) across all batches")
    print(f"{'='*80}\n")

    for i, group_info in enumerate(all_groups):
        print(f"--- Duplicate Group {i+1} ---")
        for kept, item in group_info:
            print(f"  [{kept}] [{item['timestamp'][:10]}] {item['title']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
