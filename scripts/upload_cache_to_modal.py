"""
Upload rebuilt article cache to Modal Volume.

This script uploads cache_rebuild.json to Modal Volume
at /data/processed_articles.json so the pipeline can use it.

Usage:
    modal run upload_cache_to_modal.py
"""

import json
import modal
import os

# Determine environment
ENVIRONMENT = os.getenv("WAYR_ENV", "dev")

# Create Modal app
app = modal.App(f"cache-uploader-{ENVIRONMENT}")

# Reference the same volume used by the pipeline
volume = modal.Volume.from_name(f"wayr-data-{ENVIRONMENT}", create_if_missing=True)


@app.function(volumes={"/data": volume}, timeout=300)
def upload_cache(cache_json_str: str):
    """Upload cache JSON to Modal Volume."""
    import json
    from pathlib import Path

    print("=" * 60)
    print("Uploading Cache to Modal Volume")
    print("=" * 60)

    # Parse cache JSON
    cache_data = json.loads(cache_json_str)

    print(f"Received cache data")
    print(f"  Total entries: {len(cache_data)}")
    print()

    # Write to Modal Volume
    volume_cache_file = Path("/data/processed_articles.json")

    # Ensure directory exists
    volume_cache_file.parent.mkdir(parents=True, exist_ok=True)

    with open(volume_cache_file, "w") as f:
        json.dump(cache_data, f, indent=2)

    print(f"Written cache to Modal Volume: {volume_cache_file}")
    print()

    # Commit volume changes
    volume.commit()
    print("Volume committed successfully!")
    print()

    # Verify by reading back
    with open(volume_cache_file, "r") as f:
        verified_data = json.load(f)

    print("Verification:")
    print(f"  Entries in volume: {len(verified_data)}")
    print(f"  Match original: {len(verified_data) == len(cache_data)}")
    print()

    print("=" * 60)
    print("Cache Upload Complete!")
    print("=" * 60)
    print()
    print("Next pipeline run will use this cache to prevent")
    print("re-processing of existing articles.")
    print()

    return {
        "status": "success",
        "entries": len(cache_data),
        "verified": len(verified_data) == len(cache_data),
    }


@app.local_entrypoint()
def main():
    """Local entrypoint."""
    import json
    from pathlib import Path

    # Read local cache file
    cache_file = Path("cache_rebuild.json")

    if not cache_file.exists():
        print(f"❌ Error: {cache_file} not found!")
        print("Please run rebuild_cache.py first.")
        return

    with open(cache_file, "r") as f:
        cache_json_str = f.read()

    print(f"Reading cache from {cache_file}...")
    print()

    # Upload to Modal
    result = upload_cache.remote(cache_json_str)

    if result["status"] == "success":
        print(
            f"\n✅ Successfully uploaded {result['entries']} cache entries to Modal Volume!"
        )
    else:
        print(f"\n❌ Upload failed: {result.get('message', 'Unknown error')}")
