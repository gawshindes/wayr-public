# WAYR Feature Roadmap

> Living document for planned features and architectural direction.
> Reference this before making architectural decisions to avoid rework.

---

## 1. Trending & Breaking News Prioritization

**Problem:** We publish max 5 articles per run but discover many more. No way to ensure breaking/trending news gets priority over routine stories.

**Ideas:**
- Track trend signals within existing RSS sources (same topic appearing across multiple feeds = trending)
- Recency weighting — very recent stories from multiple sources should rank higher
- Significance scoring — major launches/acquisitions > minor updates/patch notes

**Architectural implications:**
- Scoring/ranking layer needed between classification and publishing
- Trend detection from cross-feed topic overlap (dedup already partially detects this — duplicates are signal)
- May need to decouple "select top N" from "publish top N" so selection is explicit

---

## 2. Deep Research Per Article

**Problem:** Current articles summarize a single RSS source. For major stories, we should research the topic across the internet and produce comprehensive pieces.

**Ideas:**
- After identifying top articles, run internet research to gather context from multiple sources
- Example: "Meta acquires X" — research Meta's acquisition history, X's background, market impact
- Example: "Apple turns 50" — research Apple's history, timeline, milestones
- RSS becomes the **signal source** — article content comes from broader research
- Articles cite all sources gathered, not just the RSS origin

**Architectural implications:**
- New `ResearchAgent` that takes a topic and returns structured research findings
- Source URL tracking becomes critical (need to store and cite all sources)
- Content grows from ~300 words to long-form
- Better dedup needed — same topic may surface from multiple research queries
- Can extend to specialized authors per niche (see Theme 4)

---

## 3. Testing Direct Sources (PR Newswire, etc.)

**Problem:** Current RSS feeds are mostly tech blogs (TechCrunch, The Verge, etc.). Direct/primary sources like PR Newswire carry first-party announcements.

**Ideas:**
- Add PR Newswire RSS to feed list
- Validate whether the pipeline handles raw press releases well or needs different prompts
- Press releases have a different structure (formal, PR language) vs. blog posts

**Architectural implications:**
- May need source-type-aware prompting (press release vs. blog post vs. news wire)
- Independent of deep research — this is about feed coverage

---

## 4. Specialized Authors

**Problem:** One AuthorAgent writes all articles the same way regardless of topic.

**Ideas:**
- Multiple author personas per niche: Startup Author, Tech Author, AI Author
- Each with domain-specific voice, context, and depth
- Parallel research and writing per specialization

**Architectural implications:**
- AuthorAgent becomes configurable per category (prompt templates, tone, word count)
- Orchestrator routes articles to the right author based on classification
- Parallel execution possible when multiple articles are in the same batch

---

## 5. Visual & Multimedia Content

**Ideas:**
- **Images:** Add hero images to articles (source from original article, generate via AI, or use stock)
- **Video creation:** Generate short video summaries from articles
- **Podcasts:** Create podcast episodes from daily/weekly article batches
- **Weekly digests:** Aggregate and summarize the week's top stories

**Architectural implications:**
- New media pipeline stages (image sourcing → upload → attach to post)
- WordPress media upload API integration
- Storage for media assets (or generate on-the-fly)
- Podcast/video generation likely separate pipelines triggered on schedule

---

## 6. Monetization & Publishing Standards

**Problem:** Google AdSense denied placement due to policy violation. Articles must meet publishing quality standards before monetization is viable.

**Standards needed (configurable, not hardcoded):**
- **Article template/layout:** Consistent structure — headline, intro paragraph, body sections, sources, AI disclosure
- **Writing style:** Journalistic tone, factual, no promotional language
- **Word count:** Minimum/maximum per content type (news brief vs. feature)
- **SEO:** Meta descriptions, heading hierarchy (H1/H2/H3), keyword usage, alt text for images
- **AEO (Answer Engine Optimization):** Structured data (JSON-LD), FAQ sections where applicable, featured-snippet-friendly formatting
- **Ad-safe content:** No clickbait, no misleading headlines, no thin content

**Architectural implications:**
- Publishing standards config file (YAML or dedicated config, not buried in `.env`)
- AuthorAgent prompt enforces writing standards
- ProofreaderAgent validates against standards checklist
- Template system for consistent HTML article output
- Possible SEO/AEO validation step before publishing (automated checks)

---

## 7. Defining "What Is News" — Classification Philosophy

**Problem:** The classification between news and non-news needs a more principled, thoughtful definition. Current ContentType enum (news/review/guide/deal/opinion/other) catches obvious cases but misses nuance.

**Open questions:**
- **Opinion hybrids:** Analyst commentary on an announcement — is it news or opinion?
- **Significance:** Bluesky launching federation = major news; Bluesky adding a settings toggle = minor. Not all "news" deserves one of our 5 daily slots
- **What's definitively NOT news:** Product reviews disguised as news, deal roundups, "best of" lists, sponsored content, minor version bumps, routine earnings (unless notable)
- **Source credibility:** Same story from TechCrunch vs. a random blog — does source matter for classification?

**What we need:**
- Historical classification data to study patterns and iterate on definitions
- More eval test cases in `eval-classification` driven by real misclassifications
- Possibly a significance/importance score alongside content_type

**Architectural implications:**
- Strongest driver for a processed article database (classification details are currently lost after each run)
- Evaluation framework expansion — more test cases, track accuracy over time
- May need a second classification pass: content_type (news vs not) + significance (high/medium/low)

---

## 8. User Feedback Loop

**Problem:** When non-news articles slip through, there's no way for readers to flag them. Classification improves only when we manually notice mistakes.

**Ideas:**
- Allow users/editors to flag misclassified articles
- Feed flags back into classification evaluation dataset
- Track false positive rate over time

**Architectural implications:**
- Feedback endpoint or simple UI (could be WordPress comments-based initially)
- Feedback storage → feeds into eval test cases
- Closes the loop: publish → feedback → improve classification → publish better

---

## 9. Source Management

**Problem:** No record of where RSS feeds came from or how reliable each source is.

**Ideas:**
- Track "source of the source" — how/when each RSS feed was discovered
- Use historical data to score source quality (what % of articles from source X pass classification?)
- Prune unreliable sources; discover better ones

**Architectural implications:**
- RSS feed metadata (origin, date added, quality score)
- Per-source analytics (articles fetched vs. published ratio)

---

## Architectural Decision: Data Storage

### Current state

| Data | Storage | Persisted? |
|------|---------|-----------|
| Processed article URLs | `data/processed_articles.json` (hash + title + timestamp) | 30 days |
| Non-news cache | `data/non_news_cache.json` (hash + title + content_type) | 30 days |
| Social media posts | `data/twitter_posted.json`, `data/linkedin_posted.json` | Indefinite |
| Published articles | WordPress only | Indefinite |
| Classification details | Lost after run | No |
| Generated/edited content | Lost after run | No |
| LLM costs per article | Lost after run | No |
| Source URLs | Hashed — original lost | No |

### Recommended: Phased approach

**Phase 1 (soon):** Extend `processed_articles.json` to store richer metadata per article:
- Original source URL (not just hash)
- Classification result (content_type, category, confidence)
- Cost per article
- Enough to diagnose classification issues and track costs historically

**Phase 2 (when research/long-form features land):** Move to SQLite:
- Full generated/edited content
- Multiple sources per article (research yields many URLs)
- Run-level metadata (total cost, article count, errors, timestamps)
- Query capability ("show me all articles classified as Opinion last week")

**Why not Phase 2 now?** JSON files work fine for 5-10 articles/run. SQLite adds operational complexity (migrations, backups) not justified until research or feedback features need queryable history.
