"""Tests for two-tier story selection with floor/ceiling guardrails."""

from wayr.services.newsletter_selection import select_stories

KW = dict(feature_threshold=0.6, roundup_threshold=0.35, min_stories=2, max_stories=4)


def c(title, score=None):
    d = {"title": title}
    if score is not None:
        d["importance"] = {"score": score, "reasoning": "r"}
    return d


def titles(items):
    return [d["title"] for d in items]


class TestTiers:
    def test_partitions_by_threshold(self):
        sel = select_stories(
            [c("a", 0.9), c("b", 0.6), c("c", 0.5), c("d", 0.35), c("e", 0.1)], **KW
        )
        assert titles(sel.featured) == ["a", "b"]  # 0.6 is inclusive
        assert titles(sel.roundup) == ["c", "d"]  # 0.35 is inclusive
        assert titles(sel.dropped) == ["e"]
        assert sel.backfilled == 0 and sel.demoted == 0

    def test_sorted_best_first_regardless_of_input_order(self):
        sel = select_stories([c("low", 0.61), c("high", 0.95), c("mid", 0.8)], **KW)
        assert titles(sel.featured) == ["high", "mid", "low"]

    def test_unscored_bucketed_separately_never_silently_dropped(self):
        sel = select_stories([c("a", 0.9), c("no-score"), c("bad", {"x": 1})], **KW)
        assert titles(sel.unscored) == ["no-score", "bad"]
        assert titles(sel.dropped) == []

    def test_empty(self):
        sel = select_stories([], **KW)
        assert sel.counts == {
            "featured": 0,
            "roundup": 0,
            "dropped": 0,
            "unscored": 0,
            "backfilled": 0,
            "demoted": 0,
        }


class TestFloor:
    def test_backfills_best_of_roundup_to_reach_min(self):
        sel = select_stories(
            [c("a", 0.9), c("b", 0.5), c("c", 0.4), c("d", 0.36)], **KW
        )
        assert titles(sel.featured) == ["a", "b"]  # b promoted, best roundup first
        assert titles(sel.roundup) == ["c", "d"]
        assert sel.backfilled == 1

    def test_floor_stops_when_roundup_exhausted_never_promotes_dropped(self):
        sel = select_stories([c("a", 0.9), c("z", 0.1)], **KW)
        assert titles(sel.featured) == ["a"]
        assert titles(sel.dropped) == ["z"]
        assert sel.backfilled == 0

    def test_no_backfill_when_floor_already_met(self):
        sel = select_stories([c("a", 0.9), c("b", 0.7), c("c", 0.5)], **KW)
        assert titles(sel.featured) == ["a", "b"] and titles(sel.roundup) == ["c"]
        assert sel.backfilled == 0


class TestCeiling:
    def test_demotes_weakest_featured_to_front_of_roundup(self):
        many = [c(f"f{i}", 0.9 - i * 0.02) for i in range(6)] + [c("r", 0.4)]
        sel = select_stories(many, **KW)
        assert titles(sel.featured) == ["f0", "f1", "f2", "f3"]
        assert titles(sel.roundup) == ["f4", "f5", "r"]  # overflow leads the roundup
        assert sel.demoted == 2

    def test_floor_and_ceiling_interact_sanely(self):
        # min=max=3: exactly three featured whatever the day looks like
        kw = dict(KW, min_stories=3, max_stories=3)
        big = select_stories([c(f"x{i}", 0.9) for i in range(6)], **kw)
        small = select_stories(
            [c("a", 0.9), c("b", 0.5), c("c", 0.4), c("d", 0.36)], **kw
        )
        assert len(big.featured) == 3 and big.demoted == 3
        assert len(small.featured) == 3 and small.backfilled == 2
