"""List — and, only with --yes, delete — WordPress posts that are not newsletter drafts.

For turning the staging site into a demo site: everything the news pipeline ever
posted there goes; posts in categories whose name starts with "Newsletter" stay.

    poetry run python scripts/staging_cleanup.py --env-file .env.demo.base            # dry run: what would go
    poetry run python scripts/staging_cleanup.py --env-file .env.demo.base --yes      # delete (to trash)
    poetry run python scripts/staging_cleanup.py --env-file .env.demo.base --yes --force   # delete permanently

Reads WORDPRESS_URL / WORDPRESS_USERNAME / WORDPRESS_APP_PASSWORD from the env file.
Refuses to run against a URL that does not contain "staging" or "demo" unless --i-know.
"""

from __future__ import annotations

import argparse
import sys

import requests
from dotenv import dotenv_values

PAGE = 100
MAX_PAGES = 200  # 20k posts; enough for any staging site


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env-file", required=True)
    ap.add_argument(
        "--keep-prefix",
        default="Newsletter",
        help="category name prefix to keep (default: Newsletter)",
    )
    ap.add_argument("--yes", action="store_true", help="actually delete")
    ap.add_argument(
        "--force", action="store_true", help="skip the trash: permanent delete"
    )
    ap.add_argument(
        "--i-know",
        action="store_true",
        help="allow a site URL without 'staging' or 'demo' in it",
    )
    a = ap.parse_args()

    env = dotenv_values(a.env_file)
    url = (env.get("WORDPRESS_URL") or "").rstrip("/")
    auth = (
        env.get("WORDPRESS_USERNAME") or "",
        env.get("WORDPRESS_APP_PASSWORD") or "",
    )
    if not url or not all(auth):
        print(
            "WORDPRESS_URL / WORDPRESS_USERNAME / WORDPRESS_APP_PASSWORD missing",
            file=sys.stderr,
        )
        return 2
    if not a.i_know and not any(w in url for w in ("staging", "demo")):
        print(
            f"refusing to touch {url}: not a staging/demo URL (pass --i-know if it is)",
            file=sys.stderr,
        )
        return 2

    cats = {}
    r = requests.get(
        f"{url}/wp-json/wp/v2/categories",
        params={"per_page": 100},
        auth=auth,
        timeout=30,
    )
    r.raise_for_status()
    for c in r.json():
        cats[c["id"]] = c["name"]
    keep_ids = {i for i, n in cats.items() if n.startswith(a.keep_prefix)}
    print(f"categories kept: {sorted(cats[i] for i in keep_ids) or '(none)'}")

    doomed = []
    for page in range(1, MAX_PAGES + 1):
        r = requests.get(
            f"{url}/wp-json/wp/v2/posts",
            params={
                "per_page": PAGE,
                "page": page,
                "status": "any",
                "_fields": "id,title,status,categories,date",
            },
            auth=auth,
            timeout=30,
        )
        if r.status_code == 400:  # past the last page
            break
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for p in rows:
            if not (set(p.get("categories") or []) & keep_ids):
                doomed.append(p)
        if len(rows) < PAGE:
            break

    print(f"{len(doomed)} post(s) would be deleted; newsletter posts stay")
    for p in doomed[:15]:
        print(
            f"  {p['id']:>6} {p['status']:<8} {p['date'][:10]}  {p['title']['rendered'][:70]}"
        )
    if len(doomed) > 15:
        print(f"  … and {len(doomed) - 15} more")
    if not a.yes:
        print(
            "\ndry run — re-run with --yes to delete (to trash), --yes --force to delete permanently"
        )
        return 0

    n = 0
    for p in doomed:
        r = requests.delete(
            f"{url}/wp-json/wp/v2/posts/{p['id']}",
            params={"force": "true"} if a.force else None,
            auth=auth,
            timeout=30,
        )
        if r.ok:
            n += 1
        else:
            print(f"  failed {p['id']}: {r.status_code} {r.text[:80]}", file=sys.stderr)
    print(f"deleted {n} of {len(doomed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
