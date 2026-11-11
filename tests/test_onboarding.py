"""Onboarding: answers in, a reviewable folder out."""

import pytest

from wayr.onboarding import CADENCES, Answers, build, next_steps, write


@pytest.fixture
def demos(tmp_path):
    """Two throwaway demos, so the tests never read the real newsletters/ folders
    (the public release ships only newsletters/example)."""
    root = tmp_path / "newsletters"
    full = root / "tech-links"
    full.mkdir(parents=True)
    (full / "env.overrides").write_text(
        "# ---- Tech Links — appended to .env.demo-<name> by demo.sh; later keys win ----\n"
        "PIPELINE_MODE=newsletter\nMODEL_NEWSLETTER=openai:gpt-6-luna\n"
        "PLATFORM_NAME=Tech Links\nRSS_FEEDS_FILE=newsletters/tech-links/sources.txt\n"
        "NEWSLETTER_SECTIONS_FILE=newsletters/tech-links/sections.yaml\n"
        "NEWSLETTER_IMPORTANCE_CRITERIA_FILE=newsletters/tech-links/criteria.md\n"
        "NEWSLETTER_VOICE_FILE=newsletters/tech-links/voice.md\n"
        "NEWSLETTER_MAX_STORY_AGE_HOURS=96\nNEWSLETTER_MIN_STORIES=5\nNEWSLETTER_MAX_STORIES=15\n"
        "NEWSLETTER_PUBLISHER=wordpress\nNEWSLETTER_WORDPRESS_CATEGORY=Newsletter - Tech Links\n"
        "ARTICLE_CACHE_FILE=data/tech-links/processed_articles.json\n"
        "FEED_LEDGER_FILE=data/tech-links/feed_ledger.json\n"
        "NEWSLETTER_RUNS_FILE=data/tech-links/newsletter_runs.json\n"
        "WP_TAXONOMY_CACHE_FILE=data/tech-links/wp_taxonomy.json\nREGION=tech-links\n"
    )
    (full / "sections.yaml").write_text(
        "intro: false\nsections:\n  - name: Headlines & Launches\n    type: stories\n"
        "    style: feature\n    tier: feature\n    max: 7\n"
    )
    (full / "voice.md").write_text("# Voice — Tech Links\n\nTerse. No opening.\n")
    bare = root / "own-site"  # no voice.md: the packaged default stands in
    bare.mkdir()
    (bare / "env.overrides").write_text(
        "PIPELINE_MODE=newsletter\nPLATFORM_NAME=Own Site\n"
        "RSS_FEEDS_FILE=newsletters/own-site/sources.txt\nREGION=own-site\n"
    )
    return root


def answers(**over):
    base = dict(
        slug="fintech-weekly",
        name="Fintech Weekly",
        description="The week in payments and banking tech.",
        readers="Product and engineering people at fintechs.",
        cadence="weekly",
        sources=["https://a.example/feed", "https://b.example/rss  origin=brand"],
        matters=["Regulatory changes that hit payments", "Funding above $50M"],
        avoid=["Crypto price moves"],
    )
    base.update(over)
    return Answers(**base)


def test_build_from_a_demo_rewrites_identity_paths_and_cadence(demos):
    files = build(answers(), "tech-links", root=demos)
    assert set(files) == {
        "description.md",
        "sources.txt",
        "criteria.md",
        "sections.yaml",
        "voice.md",
        "env.overrides",
    }
    env = files["env.overrides"]
    assert "PLATFORM_NAME=Fintech Weekly" in env
    assert "RSS_FEEDS_FILE=newsletters/fintech-weekly/sources.txt" in env
    assert "NEWSLETTER_RUNS_FILE=data/fintech-weekly/newsletter_runs.json" in env
    assert "REGION=fintech-weekly" in env
    assert (
        f"NEWSLETTER_MAX_STORY_AGE_HOURS={CADENCES['weekly']['max_story_age_hours']}"
        in env
    )
    assert "NEWSLETTER_WORDPRESS_CATEGORY=Newsletter - Fintech Weekly" in env
    assert "MODEL_NEWSLETTER=openai:gpt-6-luna" in env  # inherited from the demo
    assert "tech-links" not in env
    # the demo's structure and voice come along; the criteria are the customer's words
    assert "Headlines & Launches" in files["sections.yaml"]
    assert "Regulatory changes that hit payments" in files["criteria.md"]
    assert "origin=brand" in files["sources.txt"]


def test_env_from_a_demo_without_a_voice_file_gains_one(demos):
    files = build(answers(), "own-site", root=demos)
    assert (
        "NEWSLETTER_VOICE_FILE=newsletters/fintech-weekly/voice.md"
        in files["env.overrides"]
    )
    assert "House voice" in files["voice.md"]


def test_voice_notes_are_appended(demos):
    files = build(answers(voice_notes="Dry humour is fine."), "tech-links", root=demos)
    assert files["voice.md"].rstrip().endswith("Dry humour is fine.")


@pytest.mark.parametrize(
    "bad",
    [
        dict(slug="Bad Slug"),
        dict(cadence="hourly"),
        dict(name=""),
        dict(sources=["not a url"]),
    ],
)
def test_bad_answers_are_loud(bad, demos):
    with pytest.raises(ValueError):
        build(answers(**bad), "tech-links", root=demos)


def test_unknown_demo_is_loud(demos):
    with pytest.raises(FileNotFoundError):
        build(answers(), "no-such-demo", root=demos)


def test_write_refuses_to_clobber_unless_told(tmp_path, demos):
    files = build(answers(), "tech-links", root=demos)
    dest = write(files, "fintech-weekly", root=tmp_path)
    assert (dest / "env.overrides").is_file()
    with pytest.raises(FileExistsError):
        write(files, "fintech-weekly", root=tmp_path)
    write(files, "fintech-weekly", root=tmp_path, overwrite=True)
    assert "./demo.sh fintech-weekly check" in next_steps("fintech-weekly")
