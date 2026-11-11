"""The Modal secret is the env file + the newsletter folder, minus what must not travel."""

from wayr.utils.deploy_env import assemble_secret, parse_env_text, redact


def test_parse_env_text_handles_comments_quotes_and_precedence():
    env = parse_env_text(
        '# note\nA=1\nB=two  # trailing comment\nC="quoted # not a comment"\nA=3\nD=   # empty, commented\n\nbad line\n'
    )
    assert env == {"A": "3", "B": "two", "C": "quoted # not a comment", "D": ""}


def test_assemble_drops_state_paths_and_mode_and_makes_newsletter_paths_absolute():
    base = {
        "OPENAI_API_KEY": "sk",
        "WORDPRESS_URL": "https://cms",
        "ARTICLE_CACHE_FILE": "data/x.json",
        "RSS_FEEDS": "a,b",
    }
    ov = {
        "PIPELINE_MODE": "newsletter",
        "RSS_FEEDS_FILE": "newsletters/wayrtoday/sources.txt",
        "NEWSLETTER_RUNS_FILE": "data/wayrtoday/runs.json",
        "NEWSLETTER_DELIVERY": "publish",
    }
    env, notes = assemble_secret(base, ov)
    assert env == {
        "OPENAI_API_KEY": "sk",
        "WORDPRESS_URL": "https://cms",
        "RSS_FEEDS": "a,b",
        "RSS_FEEDS_FILE": "/root/newsletters/wayrtoday/sources.txt",
        "NEWSLETTER_DELIVERY": "publish",
    }
    assert any("ARTICLE_CACHE_FILE" in n for n in notes)
    assert any("PIPELINE_MODE" in n for n in notes)
    assert any("made absolute" in n for n in notes)


def test_redact_hides_secrets_only():
    out = redact(
        {
            "OPENAI_API_KEY": "sk",
            "WORDPRESS_APP_PASSWORD": "p",
            "REGION": "us",
            "GOOGLE_API_KEY": "",
        }
    )
    assert out == {
        "OPENAI_API_KEY": "<set>",
        "WORDPRESS_APP_PASSWORD": "<set>",
        "REGION": "us",
        "GOOGLE_API_KEY": "",
    }


def test_assemble_warns_when_resend_is_named_but_unconfigured():
    _, notes = assemble_secret({"NEWSLETTER_MAILER": "resend"}, {})
    warnings = [n for n in notes if n.startswith("WARNING")]
    assert len(warnings) == 3
    assert any("RESEND_API_KEY" in n for n in warnings)
    _, notes = assemble_secret({"NEWSLETTER_MAILER": "none"}, {})
    assert not [n for n in notes if n.startswith("WARNING")]


def test_assemble_warns_for_resend_direct_requirements():
    _, notes = assemble_secret(
        {
            "NEWSLETTER_MAILER": "resend-direct",
            "RESEND_API_KEY": "k",
            "SUPABASE_URL": "u",
        },
        {},
    )
    missing = {
        n.split(" but ")[1].split(" is")[0] for n in notes if n.startswith("WARNING")
    }
    assert missing == {
        "NEWSLETTER_FROM",
        "NEWSLETTER_UNSUBSCRIBE_URL",
        "SUPABASE_SECRET_KEY",
    }
