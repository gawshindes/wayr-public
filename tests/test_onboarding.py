"""Onboarding: answers in, a reviewable folder out."""

import pytest

from wayr.onboarding import CADENCES, Answers, build, next_steps, write


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


def test_build_from_a_demo_rewrites_identity_paths_and_cadence():
    files = build(answers(), "tech-links")
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


def test_env_from_a_demo_without_a_voice_file_gains_one():
    files = build(answers(), "own-site")  # own-site uses the packaged default voice
    assert (
        "NEWSLETTER_VOICE_FILE=newsletters/fintech-weekly/voice.md"
        in files["env.overrides"]
    )
    assert "House voice" in files["voice.md"]


def test_voice_notes_are_appended():
    files = build(answers(voice_notes="Dry humour is fine."), "ai-daily")
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
def test_bad_answers_are_loud(bad):
    with pytest.raises(ValueError):
        build(answers(**bad), "ai-daily")


def test_unknown_demo_is_loud():
    with pytest.raises(FileNotFoundError):
        build(answers(), "no-such-demo")


def test_write_refuses_to_clobber_unless_told(tmp_path):
    files = build(answers(), "ai-daily")
    dest = write(files, "fintech-weekly", root=tmp_path)
    assert (dest / "env.overrides").is_file()
    with pytest.raises(FileExistsError):
        write(files, "fintech-weekly", root=tmp_path)
    write(files, "fintech-weekly", root=tmp_path, overwrite=True)
    assert "./demo.sh fintech-weekly check" in next_steps("fintech-weekly")
