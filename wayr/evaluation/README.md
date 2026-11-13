# Agent evaluations

One evaluator per agent, each in its own folder: `eval.py` (the checks), `test_cases.json`
(the labelled cases) and `history.json` (every run, with tokens and cost). `base_eval.py`
and `metrics.py` are shared. `poetry run wayr eval-<agent>` runs one; `eval-all` runs all.

The public release ships the evaluators without their data: the labelled cases and run
histories belong to the newsletter that produced them. To run an evaluator, write your
own `test_cases.json` next to its `eval.py`, in this shape:

```json
{
  "test_cases": [
    {
      "id": "unique-id",
      "description": "what this case checks",
      "source": {"title": "...", "url": "https://...", "source": "outlet", "content": "article text"},
      "expected": {"belongs_to_industry": true, "detected_industries": ["Software Technology"]}
    }
  ]
}
```

`source` is the article as discovery would hand it over (the classification, author and
industry/news-type/category evaluators); `dedup`, `summary` and `proofreader` take an `input`
object instead, mirroring what that agent receives. `expected` holds whatever the evaluator's
`evaluate_case` reads; the top of each `eval.py` says which keys. A run appends to
`history.json`, creating it if absent. Label a few dozen cases from your own pool, run the
evaluator once per candidate model, and read the pass rate and cost side by side.
