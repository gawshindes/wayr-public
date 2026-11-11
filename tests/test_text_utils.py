"""Tests for wayr.utils.text."""

from wayr.utils import clean_summary


def test_clean_summary_strips_tags_entities_and_whitespace():
    raw = "<p>Claude is getting  <b>Docs &amp; Slides</b>.\n\nMore [&#8230;]</p>"
    assert clean_summary(raw) == "Claude is getting Docs & Slides. More […]"


def test_clean_summary_caps_length():
    assert len(clean_summary("a" * 900, max_chars=100)) == 100


def test_clean_summary_handles_empty():
    assert clean_summary(None) == ""
    assert clean_summary("") == ""
    assert clean_summary("<br/>") == ""
