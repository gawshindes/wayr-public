# WAYR Platform Architecture

## High-Level System Architecture

```mermaid
flowchart TB
    subgraph External["External Services"]
        RSS["RSS Feeds<br/>(TechCrunch, Verge,<br/>Wired, VentureBeat)"]
        LLM["LLM Providers<br/>(OpenAI, Anthropic,<br/>Google Gemini)"]
        WP["WordPress<br/>(REST API)"]
        TW["Twitter/X<br/>(API v2)"]
        LI["LinkedIn<br/>(API)"]
        MODAL["Modal.com<br/>(Serverless)"]
    end

    subgraph Pipeline["WAYR Pipeline (Orchestrator)"]
        DA["Discovery Agent"]
        CA["Classification Agent"]
        AA["Author Agent"]
        PA["Proofreader Agent"]
        PUB["Publishing Agent"]
        SMA["Social Media Agent"]
    end

    subgraph Services["Services Layer"]
        RSSF["RSS Fetcher"]
        CE["Content Extractor"]
        WPC["WordPress Client"]
        AT["Article Tracker"]
        NNT["Non-News Tracker"]
    end

    subgraph Data["Data Models"]
        M1["RSSArticle"]
        M2["ClassifiedArticle"]
        M3["GeneratedArticle"]
        M4["EditedArticle"]
        M5["PublishedArticle"]
    end

    subgraph Storage["Persistent Storage"]
        CACHE1["processed_articles.json"]
        CACHE2["non_news_cache.json"]
        CACHE3["twitter_posted.json"]
    end

    RSS --> RSSF
    RSSF --> DA
    DA --> M1
    M1 --> CA
    LLM --> CA
    CA --> M2
    M2 --> AA
    LLM --> AA
    AA --> M3
    M3 --> PA
    LLM --> PA
    PA --> M4
    M4 --> PUB
    PUB --> WPC
    WPC --> WP
    PUB --> M5
    M5 --> SMA
    SMA --> TW
    SMA --> LI

    AT --> CACHE1
    NNT --> CACHE2
    SMA --> CACHE3

    DA --> AT
    CA --> NNT

    MODAL --> Pipeline
```

## Pipeline Data Flow

```mermaid
flowchart LR
    subgraph Step1["Step 1: Discovery"]
        RSS[("RSS Feeds")] --> Fetch["Fetch Articles"]
        Fetch --> Dedup["Deduplicate"]
        Dedup --> Filter["Filter Non-News"]
        Filter --> Extract["Extract Content"]
    end

    subgraph Step2["Step 2: Classification"]
        Extract --> Classify["Classify<br/>(L1: Content Type)"]
        Classify --> CatFilter["Filter NEWS only"]
        CatFilter --> Category["Categorize<br/>(L2: Category)"]
        Category --> ConfFilter["Confidence Filter"]
    end

    subgraph Step3["Step 3: Generation"]
        ConfFilter --> Generate["Generate Article<br/>(~300 words)"]
        Generate --> Citations["Add Citations"]
    end

    subgraph Step4["Step 4: Proofreading"]
        Citations --> Edit["Edit & Polish"]
        Edit --> CatCheck["Category Correction<br/>(if needed)"]
    end

    subgraph Step5["Step 5: Publishing"]
        CatCheck --> WP["WordPress<br/>Create Post"]
        WP --> Track["Track as<br/>Processed"]
    end

    subgraph Step6["Step 6: Social"]
        Track --> Summary["Generate<br/>Summary"]
        Summary --> Tweet["Post to<br/>Twitter"]
        Summary --> LinkedIn["Post to<br/>LinkedIn"]
    end
```

## Agent Architecture

```mermaid
classDiagram
    class BaseAgent {
        +settings: Settings
        +execute()*
        +_get_llm_response()
        +_parse_model_spec()
    }

    class NewsDiscoveryAgent {
        +rss_fetcher: RSSFetcher
        +content_extractor: ContentExtractor
        +article_tracker: ArticleTracker
        +non_news_tracker: NonNewsTracker
        +execute() List~RSSArticle~
        -_find_duplicate_indices()
    }

    class ClassificationAgent {
        +model: str
        +temperature: float
        +execute(articles) List~ClassifiedArticle~
        -_classify_single(article)
        -_build_prompt(article)
    }

    class AuthorAgent {
        +model: str
        +target_words: int
        +execute(articles) List~GeneratedArticle~
        -_generate_article(article)
    }

    class ProofreaderAgent {
        +model: str
        +execute(articles) List~EditedArticle~
        -_proofread_article(article)
    }

    class PublishingAgent {
        +wordpress_client: WordPressClient
        +article_tracker: ArticleTracker
        +execute(articles) List~PublishedArticle~
    }

    class SocialMediaAgent {
        +twitter_credentials: dict
        +summary_model: str
        +execute(articles)
        -_generate_summary(article)
        -_post_to_twitter(article)
    }

    BaseAgent <|-- NewsDiscoveryAgent
    BaseAgent <|-- ClassificationAgent
    BaseAgent <|-- AuthorAgent
    BaseAgent <|-- ProofreaderAgent
    BaseAgent <|-- PublishingAgent
    BaseAgent <|-- SocialMediaAgent
```

## Data Models

```mermaid
classDiagram
    class RSSArticle {
        +title: str
        +url: HttpUrl
        +source: str
        +published_date: datetime
        +summary: str
        +content: str
        +tags: List~str~
    }

    class ClassifiedArticle {
        +original: RSSArticle
        +content_type: ContentType
        +content_type_confidence: float
        +category: ArticleCategory
        +confidence: float
        +classification_reason: str
    }

    class GeneratedArticle {
        +classified: ClassifiedArticle
        +generated_title: str
        +generated_content: str
        +sources_cited: List~str~
        +model_used: str
    }

    class EditedArticle {
        +generated: GeneratedArticle
        +edited_title: str
        +edited_content: str
        +edit_summary: str
        +corrected_category: ArticleCategory
    }

    class PublishedArticle {
        +edited: EditedArticle
        +wordpress_post_id: int
        +wordpress_url: HttpUrl
        +published_at: datetime
    }

    class ContentType {
        <<enumeration>>
        NEWS
        REVIEW
        GUIDE
        DEAL
        OPINION
        OTHER
    }

    class ArticleCategory {
        <<enumeration>>
        STARTUP
        HARDWARE
        AI
        ENTERPRISE
        MOBILE_APPS
        GAMING
        SECURITY_PRIVACY
        CONSUMER_TECH
        GENERAL
    }

    RSSArticle --> ClassifiedArticle
    ClassifiedArticle --> GeneratedArticle
    GeneratedArticle --> EditedArticle
    EditedArticle --> PublishedArticle
    ClassifiedArticle --> ContentType
    ClassifiedArticle --> ArticleCategory
```

## Classification Logic Flow

```mermaid
flowchart TB
    Start["Article Input"] --> Step1

    subgraph Step1["STEP 1: Temporal & Context"]
        Q1{"Is this about a<br/>current, specific event?"}
        Q1 -->|"Historical/Retrospective"| CheckOp["Check if Opinion"]
        Q1 -->|"Current Event"| Step2
    end

    subgraph Step2["STEP 2: Reporting vs Interpretation"]
        Q2{"Is this reporting WHAT<br/>happened or explaining WHY/HOW?"}
        Q2 -->|"Interpretation/Analysis"| Opinion["→ OPINION"]
        Q2 -->|"Factual Reporting"| Step3
    end

    subgraph Step3["STEP 3: Primary Subject"]
        Q3{"Is TECH the<br/>primary subject?"}
        Q3 -->|"Non-tech domain"| Other["→ OTHER"]
        Q3 -->|"Consumer HW<br/>(non-major brand)"| Other
        Q3 -->|"Tech is primary"| Step4
    end

    subgraph Step4["STEP 4: Science vs Tech"]
        Q4{"Discovery or Product?"}
        Q4 -->|"Pure Science"| Other
        Q4 -->|"Commercial Tech"| Step5
    end

    subgraph Step5["STEP 5: Content Type"]
        Q5{"What type of content?"}
        Q5 -->|"Product Review"| Review["→ REVIEW"]
        Q5 -->|"How-to/Tutorial"| Guide["→ GUIDE"]
        Q5 -->|"Discount/Promo"| Deal["→ DEAL"]
        Q5 -->|"Factual Reporting"| News["→ NEWS"]
    end

    News --> Step6

    subgraph Step6["STEP 6: News Category"]
        Cat["Assign Category:<br/>Startup, Hardware, AI,<br/>Enterprise, Mobile & Apps,<br/>Gaming, Security, Consumer Tech,<br/>General"]
    end

    CheckOp -->|"Has analysis/lessons"| Opinion
    CheckOp -->|"Pure history"| Other
```

## Deployment Architecture

```mermaid
flowchart TB
    subgraph Modal["Modal.com Cloud"]
        subgraph App["wayr-platform-prod"]
            Scheduler["Cron Scheduler<br/>(Every 2 hours)"]
            Pipeline["run_wayr_pipeline()"]
            Preview["run_wayr_preview()"]
            Test["test_connections()"]
        end

        subgraph Resources["Resources"]
            CPU["2 vCPUs"]
            MEM["4GB RAM"]
            Timeout["15 min timeout"]
        end

        subgraph Volume["Persistent Volume<br/>(wayr-data-dev)"]
            Cache1[("processed_articles.json")]
            Cache2[("non_news_cache.json")]
            Cache3[("twitter_posted.json")]
        end

        Secrets["Secrets Store<br/>(wayr-secrets)"]
    end

    subgraph External["External APIs"]
        OpenAI["OpenAI API"]
        Anthropic["Anthropic API"]
        Google["Google AI API"]
        WordPress["WordPress REST API"]
        Twitter["Twitter API v2"]
    end

    Scheduler -->|"0 */2 * * *"| Pipeline
    Pipeline --> Volume
    Pipeline --> Secrets
    Secrets --> External

    Resources --> App
```

## Services Layer

```mermaid
flowchart LR
    subgraph Services["Services"]
        RSSF["RSSFetcher<br/>─────────<br/>feedparser<br/>parse feeds<br/>extract metadata"]

        CE["ContentExtractor<br/>─────────<br/>newspaper3k<br/>full article text<br/>authors, images"]

        WPC["WordPressClient<br/>─────────<br/>REST API v2<br/>HTTPBasicAuth<br/>posts, categories, tags"]

        AT["ArticleTracker<br/>─────────<br/>URL hash dedup<br/>30-day retention<br/>JSON persistence"]

        NNT["NonNewsTracker<br/>─────────<br/>Non-news cache<br/>Efficiency boost<br/>Content type tracking"]

    end

    RSS[("RSS Feeds")] --> RSSF
    URLs[("Article URLs")] --> CE
    WPC --> WP[("WordPress")]
    AT --> Cache1[("Cache JSON")]
    NNT --> Cache2[("Cache JSON")]
```

## Configuration Structure

```mermaid
flowchart TB
    subgraph Config["Settings (config.py)"]
        subgraph API["API Keys"]
            OAI["openai_api_key"]
            ANT["anthropic_api_key"]
            GOO["google_api_key"]
            TW["twitter_* (4 keys)"]
        end

        subgraph Models["Model Selection"]
            MC["model_classification<br/>(google:gemini-flash-latest)"]
            MA["model_author<br/>(openai:gpt-4)"]
            MP["model_proofreader<br/>(anthropic:claude-3-haiku)"]
            MS["model_summary<br/>(google:gemini-flash)"]
        end

        subgraph Pipeline["Pipeline Settings"]
            INT["fetch_interval_minutes"]
            MAXP["max_articles_processed"]
            MAXPUB["max_articles_published"]
            CONF["confidence_threshold"]
            PROOF["enable_proofreading"]
        end

        subgraph WP["WordPress"]
            URL["wordpress_url"]
            USER["wordpress_username"]
            PASS["wordpress_app_password"]
        end
    end

    ENV[(".env file")] --> Config
    Config --> Orchestrator["WAYROrchestrator"]
```
