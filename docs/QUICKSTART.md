# WAYR Quick Start Guide

Get WAYR up and running in 5 minutes!

## Prerequisites

- Python 3.10+
- LLM API keys (OpenAI, Google Gemini, and/or Anthropic — depending on your model config)
- WordPress site ([WordPress.com](https://wordpress.com/) or self-hosted)

## Installation

### 1. Install Poetry

```bash
# macOS/Linux
curl -sSL https://install.python-poetry.org | python3 -

# Windows (PowerShell)
(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | py -
```

### 2. Install WAYR

```bash
# Clone repository
git clone <repo-url>
cd wayr-platform

# Install dependencies
poetry install
```

### 3. Configure

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your favorite editor
nano .env  # or vim, code, etc.
```

**Minimum required settings:**
```env
OPENAI_API_KEY=sk-your-key-here
WORDPRESS_URL=https://your-site.com
WORDPRESS_USERNAME=your-username
WORDPRESS_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

> **Getting WordPress Application Password:**
> 1. Log into WordPress Admin
> 2. Go to Users → Profile
> 3. Scroll to "Application Passwords"
> 4. Enter "WAYR" and click "Add New Application Password"
> 5. Copy the generated password (includes spaces)

### 4. Test

```bash
# Test your connections
poetry run wayr test
```

You should see:
```
✓ WordPress connection successful
✓ OpenAI connection successful
```

### 5. Run

```bash
# Preview mode (doesn't publish)
poetry run wayr preview

# Full pipeline (publishes to WordPress)
poetry run wayr run
```

## That's It!

WAYR will:
1. Fetch latest tech news from RSS feeds
2. Classify articles by category
3. Generate AI-powered summaries
4. Publish to your WordPress site

## Next Steps

- Customize RSS feeds in `.env`
- Adjust article length and tone
- Set up scheduled execution (see [DEPLOYMENT.md](DEPLOYMENT.md))
- Monitor your WordPress site for new articles!

## Common Issues

### "OpenAI connection failed"
- Check your API key is correct
- Verify you have credits in your OpenAI account
- Model names are `provider:model`, e.g. `openai:gpt-6-luna`; make sure the key for that provider is set

### "WordPress connection failed"
- Verify URL includes `https://`
- Confirm REST API is enabled on your WordPress site
- Check application password is copied correctly (with spaces)
- Test manually: `curl https://your-site.com/wp-json/wp/v2/posts`

### "No articles found"
- RSS feeds might be temporarily down
- Check your internet connection
- Try running with `LOG_LEVEL=DEBUG` for more details

## Need Help?

- Read the full [README.md](../README.md)
- Check [MODAL_DEPLOYMENT.md](MODAL_DEPLOYMENT.md) for production setup
- Open an issue on GitHub

---

Happy aggregating! 🚀
