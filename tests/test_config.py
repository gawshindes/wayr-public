"""Tests for Settings — pipeline mode and per-publisher credential validation."""

import pytest
from pydantic import ValidationError

from wayr.config import MODE_AGENTS, Settings, mode_schedules

WP = {
    "wordpress_url": "https://example.com",
    "wordpress_username": "editor",
    "wordpress_app_password": "xxxx xxxx",
}
BEEHIIV = {"beehiiv_api_key": "bh-test", "beehiiv_publication_id": "pub_123"}


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Keep the developer's shell out of these tests.

    `_env_file=None` disables the repo's .env; this clears any exported vars that
    would otherwise satisfy or break the validator behind the test's back.
    """
    for var in (
        "PIPELINE_MODE",
        "WORDPRESS_URL",
        "WORDPRESS_USERNAME",
        "WORDPRESS_APP_PASSWORD",
        "BEEHIIV_API_KEY",
        "BEEHIIV_PUBLICATION_ID",
        "NEWSLETTER_MAILER",
        "RESEND_API_KEY",
        "RESEND_SEGMENT_ID",
        "NEWSLETTER_FROM",
        "NEWSLETTER_UNSUBSCRIBE_URL",
        "NEWSLETTER_TEST_RECIPIENTS",
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# The only fields with no default besides the publisher credentials under test.
BASE = {
    "openai_api_key": "sk-test",
    "industry": "Technology",
    "industry_categories": ["AI"],
}


def make(**overrides) -> Settings:
    return Settings(_env_file=None, **BASE, **overrides)


class TestNewsMode:
    def test_default_mode_is_news(self):
        assert make(**WP).pipeline_mode == "news"

    def test_news_mode_requires_wordpress(self):
        with pytest.raises(
            ValidationError, match="PIPELINE_MODE=news requires WordPress"
        ):
            make()

    def test_news_mode_names_every_missing_variable(self):
        with pytest.raises(ValidationError) as exc:
            make(wordpress_url="https://example.com")
        msg = str(exc.value)
        assert "WORDPRESS_USERNAME" in msg
        assert "WORDPRESS_APP_PASSWORD" in msg
        assert "WORDPRESS_URL" not in msg.split("missing:")[1]

    def test_news_mode_does_not_need_beehiiv(self):
        s = make(**WP)
        assert s.beehiiv_api_key is None
        assert s.beehiiv_publication_id is None


class TestNewsletterMode:
    def test_boots_without_any_wordpress_credentials(self):
        """The boot fix: a newsletter-only deployment must validate with no WordPress."""
        s = make(pipeline_mode="newsletter", **BEEHIIV)
        assert s.pipeline_mode == "newsletter"
        assert s.wordpress_url is None
        assert s.wordpress_username is None
        assert s.wordpress_app_password is None

    def test_requires_beehiiv(self):
        with pytest.raises(
            ValidationError, match="PIPELINE_MODE=newsletter requires Beehiiv"
        ):
            make(pipeline_mode="newsletter")

    def test_names_every_missing_beehiiv_variable(self):
        with pytest.raises(ValidationError) as exc:
            make(pipeline_mode="newsletter", beehiiv_api_key="bh-test")
        msg = str(exc.value)
        assert "BEEHIIV_PUBLICATION_ID" in msg
        assert "BEEHIIV_API_KEY" not in msg.split("missing:")[1]

    def test_ignores_wordpress_if_also_present(self):
        s = make(pipeline_mode="newsletter", **BEEHIIV, **WP)
        assert s.pipeline_mode == "newsletter"


class TestModeParsing:
    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            make(pipeline_mode="everything", **WP, **BEEHIIV)

    def test_mode_and_credentials_read_from_environment(self, monkeypatch):
        monkeypatch.setenv("PIPELINE_MODE", "newsletter")
        monkeypatch.setenv("BEEHIIV_API_KEY", "bh-env")
        monkeypatch.setenv("BEEHIIV_PUBLICATION_ID", "pub_env")
        s = Settings(_env_file=None, **BASE)
        assert s.pipeline_mode == "newsletter"
        assert s.beehiiv_publication_id == "pub_env"


class TestModeAgents:
    """The mode→agents table and the ENABLE_* interaction."""

    def test_news_mode_agent_set(self):
        s = make(**WP)
        assert {"author", "proofreader", "publishing", "social"} <= s.mode_agents
        assert not {"importance", "newsletter", "newsletter_publisher"} & s.mode_agents

    def test_newsletter_mode_agent_set(self):
        s = make(pipeline_mode="newsletter", **BEEHIIV)
        assert {
            "importance",
            "newsletter",
            "newsletter_publisher",
            "social",
        } <= s.mode_agents
        assert not {"author", "proofreader", "publishing"} & s.mode_agents

    def test_both_modes_share_the_research_trunk(self):
        trunk = {"discovery", "dedup", "classification", "summary"}
        assert trunk <= MODE_AGENTS["news"]
        assert trunk <= MODE_AGENTS["newsletter"]

    def test_every_mode_in_table_is_a_valid_pipeline_mode(self):
        for mode in MODE_AGENTS:
            creds = {"news": WP, "newsletter": BEEHIIV, "both": {**WP, **BEEHIIV}}[mode]
            assert make(pipeline_mode=mode, **creds).pipeline_mode == mode

    def test_ignored_flags_in_newsletter_mode_with_defaults(self):
        # author / publishing default ON → reported; proofreading defaults OFF → not;
        # social is built in newsletter mode (the issue's link), so its flag counts
        s = make(pipeline_mode="newsletter", **BEEHIIV)
        assert s.ignored_enable_flags() == ["ENABLE_AUTHOR", "ENABLE_PUBLISHING"]
        assert "social" in s.mode_agents

    def test_ignored_flags_empty_in_news_mode(self):
        assert make(**WP).ignored_enable_flags() == []

    def test_ignored_flags_only_reports_flags_that_are_on(self):
        s = make(pipeline_mode="newsletter", enable_author=False, **BEEHIIV)
        assert "ENABLE_AUTHOR" not in s.ignored_enable_flags()
        assert "ENABLE_PUBLISHING" in s.ignored_enable_flags()


class TestImportanceSettings:
    def test_criteria_default_is_none(self):
        assert (
            make(pipeline_mode="newsletter", **BEEHIIV).newsletter_importance_criteria
            is None
        )

    def test_criteria_read_from_env(self, monkeypatch):
        monkeypatch.setenv("NEWSLETTER_IMPORTANCE_CRITERIA", "Readers are CFOs.")
        s = make(pipeline_mode="newsletter", **BEEHIIV)
        assert s.newsletter_importance_criteria == "Readers are CFOs."

    def test_importance_in_newsletter_mode_only(self):
        assert "importance" in make(pipeline_mode="newsletter", **BEEHIIV).mode_agents
        assert "importance" not in make(**WP).mode_agents

    def test_criteria_file_setting(self, monkeypatch):
        monkeypatch.setenv("NEWSLETTER_IMPORTANCE_CRITERIA_FILE", "criteria.md")
        assert (
            make(
                pipeline_mode="newsletter", **BEEHIIV
            ).newsletter_importance_criteria_file
            == "criteria.md"
        )


class TestModeSchedules:
    """Deploy-time schedule selection — pure, so Modal never needs importing in tests."""

    def test_news_schedules_only_the_pipeline(self):
        assert mode_schedules("news") == {
            "pipeline": "0 */2 * * *",
            "prework": None,
            "newsletter": None,
        }

    def test_newsletter_schedules_prework_and_compile_not_pipeline(self):
        s = mode_schedules("newsletter")
        assert s["pipeline"] is None
        assert s["prework"] == "0 */2 * * *" and s["newsletter"] == "30 6 * * *"

    def test_crons_pass_through(self):
        s = mode_schedules(
            "newsletter", prework_cron="15 * * * *", newsletter_cron="30 17 * * *"
        )
        assert (s["prework"], s["newsletter"]) == ("15 * * * *", "30 17 * * *")

    def test_unknown_mode_fails_fast(self):
        with pytest.raises(ValueError, match="Unknown pipeline mode 'everything'"):
            mode_schedules("everything")

    def test_every_mode_in_the_table_has_a_schedule_plan(self):
        for mode in MODE_AGENTS:
            assert any(mode_schedules(mode).values())


class TestBothMode:
    def test_requires_wordpress_and_beehiiv(self):
        with pytest.raises(ValidationError) as exc:
            make(pipeline_mode="both", **WP)
        assert "PIPELINE_MODE=both requires WordPress and Beehiiv" in str(exc.value)
        assert "BEEHIIV_API_KEY" in str(exc.value)
        with pytest.raises(ValidationError, match="WORDPRESS_URL"):
            make(pipeline_mode="both", **BEEHIIV)

    def test_boots_with_both(self):
        s = make(pipeline_mode="both", **WP, **BEEHIIV)
        assert s.pipeline_mode == "both"

    def test_agent_set_is_the_union(self):
        s = make(pipeline_mode="both", **WP, **BEEHIIV)
        assert s.mode_agents == MODE_AGENTS["news"] | MODE_AGENTS["newsletter"]
        assert s.ignored_enable_flags() == []

    def test_schedules_pipeline_and_newsletter_not_prework(self):
        assert mode_schedules("both") == {
            "pipeline": "0 */2 * * *",
            "prework": None,
            "newsletter": "30 6 * * *",
        }


class TestNewsletterPublisherSelection:
    def test_wordpress_publisher_requires_wordpress_not_beehiiv(self):
        s = make(pipeline_mode="newsletter", newsletter_publisher="wordpress", **WP)
        assert s.newsletter_publisher == "wordpress" and s.beehiiv_api_key is None

    def test_wordpress_publisher_missing_creds_names_wordpress(self):
        with pytest.raises(
            ValidationError, match="PIPELINE_MODE=newsletter requires WordPress"
        ):
            make(
                pipeline_mode="newsletter", newsletter_publisher="wordpress", **BEEHIIV
            )

    def test_beehiiv_still_default(self):
        with pytest.raises(ValidationError, match="requires Beehiiv"):
            make(pipeline_mode="newsletter", **WP)

    def test_both_mode_with_wordpress_publisher_needs_only_wordpress(self):
        s = make(pipeline_mode="both", newsletter_publisher="wordpress", **WP)
        assert s.pipeline_mode == "both"

    def test_wordpress_category_default(self):
        assert (
            make(pipeline_mode="newsletter", **BEEHIIV).newsletter_wordpress_category
            == "Newsletter"
        )


class TestRssFeedsFile:
    def test_file_replaces_env_list_and_skips_comments_blanks_dupes(self, tmp_path):
        f = tmp_path / "sources.txt"
        f.write_text(
            "# core\nhttps://a.example/feed\n\nhttps://b.example/rss  # trade press\nhttps://a.example/feed\n"
        )
        s = make(rss_feeds_file=str(f), rss_feeds="https://ignored.example/feed", **WP)
        assert s.rss_feeds == ["https://a.example/feed", "https://b.example/rss"]

    def test_unset_leaves_rss_feeds_alone(self):
        s = make(rss_feeds="https://x.example/feed,https://y.example/feed", **WP)
        assert s.rss_feeds == ["https://x.example/feed", "https://y.example/feed"]

    def test_missing_file_is_loud(self, tmp_path):
        with pytest.raises(ValidationError, match="does not exist"):
            make(rss_feeds_file=str(tmp_path / "nope.txt"), **WP)

    def test_empty_file_is_loud(self, tmp_path):
        f = tmp_path / "sources.txt"
        f.write_text("# nothing yet\n\n")
        with pytest.raises(ValidationError, match="contains no feed URLs"):
            make(rss_feeds_file=str(f), **WP)

    def test_non_url_lines_are_rejected(self, tmp_path):
        f = tmp_path / "sources.txt"
        f.write_text("techcrunch\n")
        with pytest.raises(ValidationError, match="not a URL"):
            make(rss_feeds_file=str(f), **WP)

    def test_typed_lines(self, tmp_path):
        from wayr.models import Source

        f = tmp_path / "sources.txt"
        f.write_text(
            "https://a.example/feed\n"
            "https://openai.example/rss  origin=brand  # first-party\n"
            "https://anthropic.example/news  type=page origin=brand\n"
        )
        s = make(rss_feeds_file=str(f), **WP)
        assert s.sources == [
            Source(url="https://a.example/feed"),
            Source(url="https://openai.example/rss", origin="brand"),
            Source(url="https://anthropic.example/news", type="page", origin="brand"),
        ]
        # fetch consumers only see rss sources
        assert s.rss_feeds == ["https://a.example/feed", "https://openai.example/rss"]

    def test_bad_token_names_file_and_line(self, tmp_path):
        f = tmp_path / "sources.txt"
        f.write_text("https://a.example/feed\nhttps://b.example/feed origin=blog\n")
        with pytest.raises(ValidationError, match=r"sources\.txt:2: origin='blog'"):
            make(rss_feeds_file=str(f), **WP)

    def test_conflicting_duplicate_is_loud(self, tmp_path):
        f = tmp_path / "sources.txt"
        f.write_text("https://a.example/feed\nhttps://a.example/feed origin=brand\n")
        with pytest.raises(ValidationError, match="defined twice"):
            make(rss_feeds_file=str(f), **WP)

    def test_page_only_file_is_loud(self, tmp_path):
        f = tmp_path / "sources.txt"
        f.write_text("https://a.example/news type=page\n")
        with pytest.raises(ValidationError, match="only type=page"):
            make(rss_feeds_file=str(f), **WP)

    def test_comma_list_gives_rss_press_sources(self):
        from wayr.models import Source

        s = make(rss_feeds="https://x.example/feed,https://y.example/feed", **WP)
        assert s.sources == [
            Source(url="https://x.example/feed"),
            Source(url="https://y.example/feed"),
        ]


class TestMailer:
    """The mailer is optional; a named one needs its credentials, in newsletter modes only."""

    def test_default_is_none_and_needs_nothing(self):
        s = make(pipeline_mode="newsletter", **BEEHIIV)
        assert s.newsletter_mailer == "none"
        assert "newsletter_mailer" in s.mode_agents

    def test_resend_requires_key_segment_and_sender(self):
        with pytest.raises(ValidationError) as exc:
            make(pipeline_mode="newsletter", newsletter_mailer="resend", **BEEHIIV)
        msg = str(exc.value)
        assert "NEWSLETTER_MAILER=resend requires" in msg
        for name in ("RESEND_API_KEY", "RESEND_SEGMENT_ID", "NEWSLETTER_FROM"):
            assert name in msg

    def test_resend_validates_when_configured(self):
        s = make(
            pipeline_mode="newsletter",
            newsletter_mailer="resend",
            resend_api_key="re_k",
            resend_segment_id="seg_1",
            newsletter_from="WAYR <daily@example.com>",
            **BEEHIIV,
        )
        assert s.newsletter_mailer == "resend"

    def test_news_mode_ignores_the_mailer(self):
        s = make(newsletter_mailer="resend", **WP)
        assert "newsletter_mailer" not in s.mode_agents

    def test_resend_direct_requires_list_and_unsubscribe_link(self):
        with pytest.raises(ValidationError) as exc:
            make(
                pipeline_mode="newsletter", newsletter_mailer="resend-direct", **BEEHIIV
            )
        msg = str(exc.value)
        assert "NEWSLETTER_MAILER=resend-direct requires" in msg
        for name in (
            "RESEND_API_KEY",
            "NEWSLETTER_FROM",
            "NEWSLETTER_UNSUBSCRIBE_URL",
            "SUPABASE_URL",
            "SUPABASE_SECRET_KEY",
        ):
            assert name in msg
        assert "RESEND_SEGMENT_ID" not in msg

    def test_test_recipients_parse(self):
        s = make(
            pipeline_mode="newsletter",
            newsletter_test_recipients=" a@x.com, b@x.com ,, ",
            **BEEHIIV,
        )
        assert s.newsletter_test_recipient_list == ["a@x.com", "b@x.com"]


class TestSupabaseKeyName:
    def test_new_name_and_legacy_alias_both_populate_the_field(self, monkeypatch):
        assert (
            make(**WP, supabase_secret_key="sb_secret_1").supabase_secret_key
            == "sb_secret_1"
        )
        assert (
            make(**WP, supabase_service_role_key="eyJ.legacy").supabase_secret_key
            == "eyJ.legacy"
        )
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "from-env-legacy")
        assert make(**WP).supabase_secret_key == "from-env-legacy"
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "from-env-new")
        assert make(**WP).supabase_secret_key == "from-env-new"
