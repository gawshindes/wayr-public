"""Per-model prices, so token counts become dollars.

USD per 1M tokens: (input, cached input, output). Reasoning tokens are billed
as output by OpenAI and are included in completion_tokens. Unknown models cost
None — never a guess.
"""

from __future__ import annotations

from typing import Optional, Tuple

PRICES_PER_1M: dict = {
    # OpenAI (developers.openai.com/api/docs/models, 2026-09-23)
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
}


def price_for(model: str) -> Optional[Tuple[float, float, float]]:
    """Exact id first, then the dated-snapshot form ('gpt-5.4-nano-2026-03-17')."""
    if model in PRICES_PER_1M:
        return PRICES_PER_1M[model]
    for key, p in PRICES_PER_1M.items():
        if model.startswith(key + "-"):
            return p
    return None


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
) -> Optional[float]:
    p = price_for(model)
    if p is None:
        return None
    in_price, cached_price, out_price = p
    uncached = max(0, input_tokens - cached_input_tokens)
    return (
        uncached * in_price
        + cached_input_tokens * cached_price
        + output_tokens * out_price
    ) / 1_000_000
