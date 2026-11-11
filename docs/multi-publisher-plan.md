# Multi-Publisher Support (WordPress, Beehiiv, future Ghost)

> **Status:** deferred — saved as a future-feature plan. Pick this up when there's a real ask for a second publisher.

## Context

WAYR currently publishes only to WordPress. The `PublishingAgent` directly instantiates a `WordPressClient`, and `PublishedArticle` carries WordPress-specific fields (`wordpress_post_id`, `wordpress_url`). We want to add **Beehiiv** as a second publisher (draft-mode posts via API, no email send) and leave a clean seam for **Ghost** later. Per design decisions:

- **Routing:** one active publisher per run, selected by env var (`PUBLISHER=wordpress|beehiiv`).
- **Config:** flat env vars with publisher prefix (`WORDPRESS_*`, `BEEHIIV_*`, later `GHOST_*`).
- **Beehiiv mode:** draft posts only (`status=draft`); no email send for now.
- **Model rename:** clean break to `publisher_post_id` / `published_url` + new `publisher_platform` field.

## Approach

Use an **adapter pattern**: each publisher is a self-contained adapter file under `wayr/publishers/` that implements a single `PublisherAdapter` protocol. `PublishingAgent` becomes a generic orchestrator that knows nothing about specific platforms — it just calls the adapter selected by config. **Adding a new publisher in the future = one new adapter file + one line in the registry + a few env vars.** No changes to the agent, orchestrator, or models.

### 1. Define the `PublisherAdapter` protocol — new file [wayr/publishers/base.py](../wayr/publishers/base.py)

The adapter contract is the *only* thing the rest of the codebase knows about. Every publisher implements exactly this:

```python
class PublishResult(BaseModel):
    publisher_platform: str       # "wordpress" | "beehiiv" | "ghost"
    publisher_post_id: str        # str (WP returns int, Beehiiv returns str-id) — normalize
    published_url: HttpUrl
    status: str                   # "publish" | "draft" | ...

class PublisherAdapter(ABC):
    platform: ClassVar[str]                              # "wordpress", "beehiiv", ...

    @classmethod
    @abstractmethod
    def from_settings(cls, settings: Settings) -> "PublisherAdapter":
        """Build the adapter from env-loaded settings. Raises if creds missing."""

    @abstractmethod
    async def publish(self, article: EditedArticle) -> PublishResult: ...

    @abstractmethod
    def test_connection(self) -> bool: ...
```

The `from_settings` classmethod keeps each adapter responsible for its own config wiring — the factory just calls it. No retries / LLM machinery here; this is orthogonal to `BaseAgent` (which is LLM-only).

### 2. Move existing WordPress code behind the protocol

- Rename current [wayr/services/wordpress_client.py](../wayr/services/wordpress_client.py) to [wayr/publishers/wordpress.py](../wayr/publishers/wordpress.py) and wrap it in a `WordPressPublisher(PublisherAdapter)` class.
- The existing `_publish_single` body in [wayr/agents/publishing.py:75-130](../wayr/agents/publishing.py#L75-L130) (category resolution, tag-from-source, excerpt extraction, REST POST) moves into `WordPressPublisher.publish()`.
- `WordPressPublisher.test_connection()` keeps the existing `/wp-json/wp/v2/posts` GET check.
- Taxonomy cache file path stays at `WP_TAXONOMY_CACHE_FILE`.

### 3. Implement `BeehiivPublisher` — new file [wayr/publishers/beehiiv.py](../wayr/publishers/beehiiv.py)

- Endpoint: `POST https://api.beehiiv.com/v2/publications/{publication_id}/posts`
- Auth: `Authorization: Bearer {BEEHIIV_API_KEY}`
- Body: `title`, `body_content` (HTML from `edited_content`), `status: "draft"`, `content_tags: [article.source]`, plus `subtitle` from the LLM-generated excerpt.
- No category mapping for v1 (Beehiiv uses content tags only) — secondary category becomes a second tag.
- Response: extract `id` and `web_url` (or build `https://app.beehiiv.com/posts/{id}`) into `PublishResult`.
- `test_connection()`: `GET /v2/publications/{publication_id}` and check 200.

### 4. Add `GhostPublisher` stub — new file [wayr/publishers/ghost.py](../wayr/publishers/ghost.py)

Empty class that raises `NotImplementedError("Ghost publisher coming soon")` — proves the seam works without doing the work.

### 5. Adapter registry + factory — new file [wayr/publishers/__init__.py](../wayr/publishers/__init__.py)

A simple name → adapter-class registry so the factory has zero `if/elif` ladder. Adding a publisher = one line.

```python
from wayr.publishers.base import PublisherAdapter
from wayr.publishers.wordpress import WordPressPublisher
from wayr.publishers.beehiiv import BeehiivPublisher
from wayr.publishers.ghost import GhostPublisher

REGISTRY: dict[str, type[PublisherAdapter]] = {
    WordPressPublisher.platform: WordPressPublisher,
    BeehiivPublisher.platform:   BeehiivPublisher,
    GhostPublisher.platform:     GhostPublisher,
}

def get_publisher(settings: Settings) -> PublisherAdapter:
    name = settings.publisher.lower()
    if name not in REGISTRY:
        raise ValueError(f"Unknown publisher '{name}'. Available: {list(REGISTRY)}")
    return REGISTRY[name].from_settings(settings)
```

### 6. Refactor `PublishingAgent` — [wayr/agents/publishing.py](../wayr/agents/publishing.py)

- Constructor takes a `PublisherAdapter` (DI), no longer builds `WordPressClient`.
- `publish_articles()` loops, calls `publisher.publish(article)`, wraps the `PublishResult` into `PublishedArticle`. Per-article try/except stays.
- `test_connection()` delegates to `publisher.test_connection()`.
- All WordPress-specific code (categories, tags, excerpt) is gone from this file — it lives in `WordPressPublisher`.

### 7. Generalize `PublishedArticle` — [wayr/models/articles.py:276-298](../wayr/models/articles.py#L276-L298)

Replace:
```python
wordpress_post_id: int
wordpress_url: HttpUrl
```
with:
```python
publisher_platform: str       # "wordpress" | "beehiiv" | "ghost"
publisher_post_id: str        # str — accommodates non-int IDs
published_url: HttpUrl
```

Clean break — no aliases. Keep `published_at` and `status`.

### 8. Config changes — [wayr/config.py](../wayr/config.py)

Add to the `Settings` class (around lines 267–273):

```python
publisher: str = Field(default="wordpress", description="Active publisher: wordpress|beehiiv|ghost")

# WordPress (existing — unchanged)
wordpress_url: HttpUrl | None = None
wordpress_username: str | None = None
wordpress_app_password: str | None = None

# Beehiiv (new)
beehiiv_api_key: str | None = None
beehiiv_publication_id: str | None = None

# Ghost (placeholder for future)
ghost_url: HttpUrl | None = None
ghost_admin_api_key: str | None = None
```

Make existing WordPress fields **optional** (currently required) so the app can boot with only Beehiiv configured. Add a `model_validator` that requires the credentials of whichever publisher is active.

Update `.env.example` (and any `.env.india` / `.env.staging` references in CLAUDE.md) with the new keys and a comment about `PUBLISHER=`.

### 9. Orchestrator wiring — [wayr/orchestrator.py:177](../wayr/orchestrator.py#L177)

Replace direct `PublishingAgent(WordPressClient(...))` construction with:
```python
publisher = get_publisher(settings)
self.publishing_agent = PublishingAgent(publisher)
```
No changes to the call site at line 591–592.

### 10. SocialMediaAgent — [wayr/agents/social_media.py](../wayr/agents/social_media.py)

Replace every `article.wordpress_url` reference with `article.published_url`. (Search confirmed it only reads the URL — no platform-specific logic.)

### 11. CLI — [wayr/cli.py](../wayr/cli.py)

- `wayr test` (lines 238–253): test message becomes "Testing {settings.publisher} connection…" — implementation already delegates through `publishing_agent.test_connection()`, which now hits the active publisher.
- `wayr config` (lines 257–293): print `Publisher: {settings.publisher}` and only the credentials block for the active publisher (URL/username for WordPress; publication_id for Beehiiv; password/API key always masked).

### 12. Evaluation / fixtures

Quick grep for `wordpress_post_id` / `wordpress_url` across `wayr/evaluation/`, `tests/`, and `data/` — rename any references. Eval test cases shouldn't reference these (they evaluate pre-publish stages), but verify.

## Adding a future publisher (the adapter contract in practice)

Once this lands, supporting (e.g.) Substack or Medium is a closed three-step change with no edits to `PublishingAgent`, the orchestrator, models, or the social media agent:

1. **Drop in an adapter file** at `wayr/publishers/substack.py` implementing `PublisherAdapter` (`platform`, `from_settings`, `publish`, `test_connection`).
2. **Register it** — one line in `wayr/publishers/__init__.py`'s `REGISTRY` dict.
3. **Add env vars** — `SUBSTACK_*` in `Settings` + `.env.example`, plus a branch in the active-publisher validator.

Activate with `PUBLISHER=substack` in the env. That's the entire surface area.

## Critical Files

- [wayr/agents/publishing.py](../wayr/agents/publishing.py) — gutted, becomes thin delegator
- [wayr/services/wordpress_client.py](../wayr/services/wordpress_client.py) → moves to `wayr/publishers/wordpress.py`
- `wayr/publishers/base.py` — new protocol + `PublishResult`
- `wayr/publishers/beehiiv.py` — new
- `wayr/publishers/__init__.py` — factory
- [wayr/models/articles.py](../wayr/models/articles.py) — `PublishedArticle` field rename
- [wayr/config.py](../wayr/config.py) — `publisher` selector + Beehiiv/Ghost vars + validator
- [wayr/orchestrator.py](../wayr/orchestrator.py) — use factory
- [wayr/agents/social_media.py](../wayr/agents/social_media.py) — field rename
- [wayr/cli.py](../wayr/cli.py) — config/test output
- `.env.example` — new vars

## Verification

1. **Unit-ish smoke:** `poetry run pytest` passes (existing tests reference `PublishedArticle` — they'll need rename touch-ups; this is the canary that catches misses).
2. **Type check:** `poetry run mypy wayr/` — catches any leftover `wordpress_url` references.
3. **WordPress regression** (must still work):
   - `PUBLISHER=wordpress poetry run wayr test` → connection OK
   - `PUBLISHER=wordpress poetry run wayr preview` → runs to completion (no actual publish)
   - End-to-end `poetry run wayr run --skip-publishing` to confirm pipeline still produces `EditedArticle`s (publishing pathway unchanged from caller's view).
4. **Beehiiv smoke:**
   - Set `PUBLISHER=beehiiv`, `BEEHIIV_API_KEY=...`, `BEEHIIV_PUBLICATION_ID=...`
   - `poetry run wayr test` → 200 from `GET /publications/{id}`
   - `poetry run wayr run` against a single throwaway feed → confirm a draft post lands in Beehiiv UI with title, HTML body, source-name tag.
5. **Config display:** `PUBLISHER=beehiiv poetry run wayr config` shows only Beehiiv fields (no WordPress fields), and `PUBLISHER=wordpress` shows only WordPress.
6. **Social media regression:** confirm `SocialMediaAgent` reads `published_url` and posts the new URL whether it came from WordPress or Beehiiv.
