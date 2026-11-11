"""Typed sources: the sources-file line format."""

import pytest

from wayr.models import Source, parse_source_line, parse_sources_text, sources_from_urls

U = "https://example.com/feed"


class TestParseLine:
    def test_plain_url_is_rss_press(self):
        assert parse_source_line(U) == Source(url=U, type="rss", origin="press")

    def test_blank_and_comment_lines_are_none(self):
        assert parse_source_line("") is None
        assert parse_source_line("   # just a note") is None

    @pytest.mark.parametrize(
        "line, expected",
        [
            (f"{U} origin=brand", Source(url=U, origin="brand")),
            (f"{U} type=page", Source(url=U, type="page")),
            (
                f"{U} type=page origin=brand",
                Source(url=U, type="page", origin="brand"),
            ),
            (
                f"{U}   origin=brand   type=page",
                Source(url=U, type="page", origin="brand"),
            ),
            (f"{U} origin=brand  # OpenAI's own feed", Source(url=U, origin="brand")),
            (f"{U}#comment-glued", Source(url=U)),
        ],
    )
    def test_tokens(self, line, expected):
        assert parse_source_line(line) == expected

    @pytest.mark.parametrize(
        "line, msg",
        [
            ("techcrunch", "not a URL"),
            (f"{U} brand", "expected key=value"),
            (f"{U} origin:brand", "expected key=value"),
            (f"{U} foo=bar", "unknown key"),
            (f"{U} type=atom", "type='atom' is not one of"),
            (f"{U} origin=blog", "origin='blog' is not one of"),
            (f"{U} origin=press origin=brand", "given twice"),
        ],
    )
    def test_bad_lines_are_loud(self, line, msg):
        with pytest.raises(ValueError, match=msg):
            parse_source_line(line, lineno=7, filename="sources.txt")

    def test_error_names_file_and_line(self):
        with pytest.raises(ValueError, match=r"^sources\.txt:7: "):
            parse_source_line(f"{U} foo=bar", lineno=7, filename="sources.txt")


class TestParseText:
    def test_identical_repeat_collapses_and_order_is_kept(self):
        text = f"# a\n{U}\n\nhttps://b.example/rss  origin=brand\n{U}\n"
        assert parse_sources_text(text) == [
            Source(url=U),
            Source(url="https://b.example/rss", origin="brand"),
        ]

    def test_conflicting_repeat_raises(self):
        with pytest.raises(ValueError, match="defined twice with different tokens"):
            parse_sources_text(f"{U}\n{U} origin=brand\n", filename="s.txt")

    def test_empty_text_is_empty_list(self):
        assert parse_sources_text("# nothing\n\n") == []


class TestRoundTrip:
    @pytest.mark.parametrize(
        "src",
        [
            Source(url=U),
            Source(url=U, origin="brand"),
            Source(url=U, type="page"),
            Source(url=U, type="page", origin="brand"),
        ],
    )
    def test_to_line_parses_back(self, src):
        assert parse_source_line(src.to_line()) == src

    def test_plain_line_has_no_tokens(self):
        assert Source(url=U).to_line() == U


def test_sources_from_urls():
    assert sources_from_urls([" https://a ", "https://b", "https://a", ""]) == [
        Source(url="https://a"),
        Source(url="https://b"),
    ]
