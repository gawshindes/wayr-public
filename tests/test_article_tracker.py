class TestUnclassified:
    def test_only_discovered_stage_within_window(self, tmp_path):
        from datetime import datetime, timezone

        from wayr.models import RSSArticle
        from wayr.services.article_tracker import ArticleTracker

        t = ArticleTracker(cache_file=str(tmp_path / "p.json"))
        a = RSSArticle(
            title="A",
            url="https://x/a",
            source="S",
            published_date=datetime.now(timezone.utc),
            content="w " * 60,
        )
        b = RSSArticle(
            title="B",
            url="https://x/b",
            source="S",
            published_date=datetime.now(timezone.utc),
            content="w " * 60,
        )
        t.record_discovered([a, b])
        assert sorted(e["title"] for e in t.get_unclassified()) == ["A", "B"]
        assert all("url_hash" in e for e in t.get_unclassified())
        # anything that advanced is no longer stuck
        t.processed[t._get_article_hash(a)]["last_stage"] = "industry"
        assert [e["title"] for e in t.get_unclassified()] == ["B"]
        # and the window excludes old entries
        t.processed[t._get_article_hash(b)]["created_at"] = "2020-01-01T00:00:00+00:00"
        assert t.get_unclassified(max_age_hours=48) == []
