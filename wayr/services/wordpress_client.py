"""WordPress REST API client."""

import json
import re
import requests
from html import unescape
from pathlib import Path
from typing import Any, Dict, Optional
from requests.auth import HTTPBasicAuth
from wayr.utils import get_logger

logger = get_logger(__name__)


class WordPressClient:
    """Client for WordPress REST API."""

    def __init__(
        self,
        site_url: str,
        username: str,
        app_password: str,
        taxonomy_cache_file: str = "data/wp_taxonomy.json",
    ):
        """
        Initialize WordPress client.

        Args:
            site_url: WordPress site URL (e.g., https://wayr.today)
            username: WordPress username
            app_password: WordPress application password
            taxonomy_cache_file: Path to persistent name→ID cache for categories/tags
        """
        self.site_url = site_url.rstrip("/")
        self.api_url = f"{self.site_url}/wp-json/wp/v2"
        self.auth = HTTPBasicAuth(username, app_password)
        self.taxonomy_cache_file = Path(taxonomy_cache_file)
        self._taxonomy = self._load_taxonomy()

    def _load_taxonomy(self) -> Dict[str, Dict[str, int]]:
        """Load persistent taxonomy cache from disk. Returns {"categories": {...}, "tags": {...}}."""
        if not self.taxonomy_cache_file.exists():
            return {"categories": {}, "tags": {}}
        try:
            with open(self.taxonomy_cache_file, "r") as f:
                data = json.load(f)
            data.setdefault("categories", {})
            data.setdefault("tags", {})
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read taxonomy cache, starting fresh: {e}")
            return {"categories": {}, "tags": {}}

    def _save_taxonomy(self) -> None:
        """Persist taxonomy cache to disk."""
        try:
            self.taxonomy_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.taxonomy_cache_file, "w") as f:
                json.dump(self._taxonomy, f, indent=2)
        except OSError as e:
            logger.warning(f"Could not persist taxonomy cache: {e}")

    def create_post(
        self,
        title: str,
        content: str,
        status: str = "publish",
        categories: Optional[list] = None,
        tags: Optional[list] = None,
        excerpt: Optional[str] = None,
    ) -> Dict:
        """
        Create a new WordPress post.

        Args:
            title: Post title
            content: Post content (HTML)
            status: Post status (publish, draft, pending, private)
            categories: List of category IDs
            tags: List of tag IDs
            excerpt: Post excerpt

        Returns:
            WordPress post response data

        Raises:
            Exception: If post creation fails
        """
        endpoint = f"{self.api_url}/posts"

        post_data: Dict[str, Any] = {
            "title": title,
            "content": content,
            "status": status,
        }

        if categories:
            post_data["categories"] = categories
        if tags:
            post_data["tags"] = tags
        if excerpt:
            post_data["excerpt"] = excerpt

        try:
            logger.info(f"Creating WordPress post: {title}")
            response = requests.post(
                endpoint, json=post_data, auth=self.auth, timeout=30
            )
            response.raise_for_status()

            post = response.json()
            logger.info(
                f"Post created successfully: ID={post.get('id')}, URL={post.get('link')}"
            )

            return post

        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating WordPress post: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            raise

    def update_post(self, post_id: int, **kwargs) -> Dict:
        """
        Update an existing WordPress post.

        Args:
            post_id: WordPress post ID
            **kwargs: Fields to update (title, content, status, etc.)

        Returns:
            WordPress post response data
        """
        endpoint = f"{self.api_url}/posts/{post_id}"

        try:
            logger.info(f"Updating WordPress post: {post_id}")
            response = requests.post(endpoint, json=kwargs, auth=self.auth, timeout=30)
            response.raise_for_status()

            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Error updating WordPress post: {e}")
            raise

    def get_post(self, post_id: int) -> Dict:
        """
        Get a WordPress post by ID.

        Args:
            post_id: WordPress post ID

        Returns:
            WordPress post data
        """
        endpoint = f"{self.api_url}/posts/{post_id}"

        try:
            response = requests.get(endpoint, auth=self.auth, timeout=30)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting WordPress post: {e}")
            raise

    def get_categories(self) -> list:
        """
        Get all WordPress categories (handles pagination).

        Returns:
            List of category objects
        """
        endpoint = f"{self.api_url}/categories"
        all_categories = []
        page = 1

        try:
            while True:  # bounded-loop-ok (breaks when no more pages from WP API)
                # Fetch categories with pagination (100 per page)
                response = requests.get(
                    endpoint,
                    params={"per_page": 100, "page": page},
                    auth=self.auth,
                    timeout=30,
                )
                response.raise_for_status()
                categories = response.json()

                if not categories:
                    break

                all_categories.extend(categories)

                # Check if there are more pages
                total_pages = int(response.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break

                page += 1

            logger.info(f"Fetched {len(all_categories)} categories from WordPress")
            return all_categories

        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting categories: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
            return []

    def get_or_create_category(self, name: str) -> int:
        """
        Get category ID by name, or create if it doesn't exist.

        Uses a persistent name→ID cache. On cache miss, fetches from WordPress
        and writes the resolved ID back to the cache.

        Args:
            name: Category name

        Returns:
            Category ID
        """
        cache_key = name.lower()
        cached_id = self._taxonomy["categories"].get(cache_key)
        if cached_id is not None:
            return cached_id

        categories = self.get_categories()
        category_names = [cat.get("name", "") for cat in categories]
        logger.info(f"Looking for category '{name}' in: {category_names}")

        matched_id = None
        for cat in categories:
            wp_name = unescape(cat.get("name", "")).lower()
            self._taxonomy["categories"][wp_name] = cat["id"]
            if wp_name == cache_key:
                matched_id = cat["id"]

        if categories:
            self._save_taxonomy()

        if matched_id is not None:
            logger.info(f"Found existing category: {name} (ID: {matched_id})")
            return matched_id

        endpoint = f"{self.api_url}/categories"
        try:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            logger.info(f"Creating new category: {name} (slug: {slug})")
            response = requests.post(
                endpoint, json={"name": name, "slug": slug}, auth=self.auth, timeout=30
            )
            response.raise_for_status()
            category_id = response.json()["id"]
            logger.info(f"Category created: {name} (ID: {category_id})")
            self._taxonomy["categories"][cache_key] = category_id
            self._save_taxonomy()
            return category_id

        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating category '{name}': {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
            raise

    def get_tags(self) -> list:
        """
        Get all WordPress tags (handles pagination).

        Returns:
            List of tag objects
        """
        endpoint = f"{self.api_url}/tags"
        all_tags = []
        page = 1

        try:
            while True:  # bounded-loop-ok (breaks when no more pages from WP API)
                # Fetch tags with pagination (100 per page)
                response = requests.get(
                    endpoint,
                    params={"per_page": 100, "page": page},
                    auth=self.auth,
                    timeout=30,
                )
                response.raise_for_status()
                tags = response.json()

                if not tags:
                    break

                all_tags.extend(tags)

                # Check if there are more pages
                total_pages = int(response.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break

                page += 1

            logger.info(f"Fetched {len(all_tags)} tags from WordPress")
            return all_tags

        except requests.exceptions.RequestException as e:
            logger.error(f"Error getting tags: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
            return []

    def get_or_create_tag(self, name: str) -> int:
        """
        Get tag ID by name, or create if it doesn't exist.

        Uses a persistent name→ID cache. On cache miss, fetches from WordPress
        and writes the resolved ID back to the cache.

        Args:
            name: Tag name (e.g., "TechCrunch", "The Verge")

        Returns:
            Tag ID
        """
        cache_key = name.lower()
        cached_id = self._taxonomy["tags"].get(cache_key)
        if cached_id is not None:
            return cached_id

        tags = self.get_tags()

        matched_id = None
        for tag in tags:
            wp_name = unescape(tag.get("name", "")).lower()
            self._taxonomy["tags"][wp_name] = tag["id"]
            if wp_name == cache_key:
                matched_id = tag["id"]

        if tags:
            self._save_taxonomy()

        if matched_id is not None:
            logger.info(f"Found existing tag: {name} (ID: {matched_id})")
            return matched_id

        endpoint = f"{self.api_url}/tags"
        try:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            logger.info(f"Creating new tag: {name} (slug: {slug})")
            response = requests.post(
                endpoint, json={"name": name, "slug": slug}, auth=self.auth, timeout=30
            )
            response.raise_for_status()
            tag_id = response.json()["id"]
            logger.info(f"Tag created: {name} (ID: {tag_id})")
            self._taxonomy["tags"][cache_key] = tag_id
            self._save_taxonomy()
            return tag_id

        except requests.exceptions.RequestException as e:
            # Handle "term_exists" error — tag already exists but get_tags() failed earlier
            if hasattr(e, "response") and e.response is not None:
                try:
                    error_data = e.response.json()
                    if error_data.get("code") == "term_exists":
                        term_id = error_data.get("data", {}).get("term_id")
                        if term_id:
                            logger.info(
                                f"Tag '{name}' already exists (ID: {term_id}), using existing"
                            )
                            self._taxonomy["tags"][cache_key] = term_id
                            self._save_taxonomy()
                            return term_id
                except (ValueError, KeyError):
                    pass
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
            logger.error(f"Error creating tag '{name}': {e}")
            raise

    def test_connection(self) -> bool:
        """
        Test WordPress API connection.

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            response = requests.get(f"{self.api_url}/posts", auth=self.auth, timeout=10)
            response.raise_for_status()
            logger.info("WordPress connection test successful")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"WordPress connection test failed: {e}")
            return False
