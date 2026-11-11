"""Token accounting and cost estimates."""

from types import SimpleNamespace

from wayr.utils.pricing import estimate_cost_usd, price_for


def test_price_lookup_exact_and_dated():
    assert price_for("gpt-6-luna") == (0.10, 0.01, 0.50)
    assert price_for("gpt-5.4-nano-2026-03-17") == price_for("gpt-5.4-nano")
    assert price_for("gemini-flash-latest") is None


def test_cost_estimate_uses_cached_price_and_bills_reasoning_as_output():
    # 1M input of which 200k cached, 100k output (reasoning is inside output)
    cost = estimate_cost_usd(
        "gpt-6-luna", 1_000_000, 100_000, cached_input_tokens=200_000
    )
    assert abs(cost - (800_000 * 0.10 + 200_000 * 0.01 + 100_000 * 0.50) / 1e6) < 1e-9
    assert estimate_cost_usd("unknown-model", 10, 10) is None


def test_usage_stats_capture_reasoning_and_cached_tokens():
    from wayr.agents.base import UsageStats

    u = UsageStats()
    # mimic what _call_openai does with a response.usage object
    resp = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=400,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=350),
        prompt_tokens_details=SimpleNamespace(cached_tokens=600),
    )
    u.input_tokens += resp.prompt_tokens
    u.output_tokens += resp.completion_tokens
    u.reasoning_tokens += resp.completion_tokens_details.reasoning_tokens
    u.cached_input_tokens += resp.prompt_tokens_details.cached_tokens
    u.calls += 1
    assert u.as_dict() == {
        "calls": 1,
        "input_tokens": 1000,
        "cached_input_tokens": 600,
        "output_tokens": 400,
        "reasoning_tokens": 350,
    }


def test_orchestrator_usage_report_and_evaluator_summary(tmp_path, monkeypatch):
    from tests.test_orchestrator_research import build

    o = build(tmp_path, monkeypatch)
    # pretend two agents made calls
    o.news_classifier_agent.usage.calls = 3
    o.news_classifier_agent.usage.input_tokens = 3000
    o.news_classifier_agent.usage.output_tokens = 300
    o.dedup_agent.usage.calls = 1
    o.dedup_agent.usage.input_tokens = 500
    o.dedup_agent.usage.output_tokens = 50
    rows = {r["agent"]: r for r in o.usage_report()}
    assert set(rows) == {"NewsClassifierAgent", "DeduplicationAgent"}
    assert rows["NewsClassifierAgent"]["calls"] == 3
    # cost is None or a float depending on whether the fixture's model is priced
    assert (
        rows["DeduplicationAgent"]["cost_usd"] is None
        or rows["DeduplicationAgent"]["cost_usd"] >= 0
    )

    from wayr.evaluation.base_eval import BaseEvaluator

    ev = BaseEvaluator.__new__(BaseEvaluator)
    ev.dedup_agent = o.dedup_agent  # any attribute name works
    s = ev.usage_summary()
    assert s["calls"] == 1 and s["input_tokens"] == 500
