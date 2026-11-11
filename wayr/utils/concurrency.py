"""Bounded-concurrency helpers for pipeline fan-out.

Discovery and classification both apply an async operation across a batch of
articles. Run serially those batches dominate wall-clock time; run unbounded
they open one HTTP request or LLM call per article at once. `map_concurrent`
sits in between: at most `limit` operations in flight, results in input order.
"""

from __future__ import annotations

import asyncio
from typing import (
    Awaitable,
    Callable,
    List,
    Literal,
    Sequence,
    TypeVar,
    Union,
    overload,
)

T = TypeVar("T")
ItemT = TypeVar("ItemT")


@overload
async def map_concurrent(
    items: Sequence[ItemT],
    fn: Callable[[ItemT], Awaitable[T]],
    *,
    limit: int,
    return_exceptions: Literal[False] = False,
) -> List[T]: ...


@overload
async def map_concurrent(
    items: Sequence[ItemT],
    fn: Callable[[ItemT], Awaitable[T]],
    *,
    limit: int,
    return_exceptions: Literal[True],
) -> List[Union[T, BaseException]]: ...


async def map_concurrent(
    items: Sequence[ItemT],
    fn: Callable[[ItemT], Awaitable[T]],
    *,
    limit: int,
    return_exceptions: bool = False,
):
    """Apply `fn` to every item with at most `limit` calls in flight.

    Args:
        items: Inputs to map over.
        fn: Async callable applied to each item.
        limit: Maximum number of concurrent calls. Values < 1 are treated as 1.
        return_exceptions: When True, a failing item yields the exception in
            its slot instead of aborting the batch.

    Returns:
        Results in the same order as `items` — callers may zip the two together.
    """
    if not items:
        return []

    semaphore = asyncio.Semaphore(max(1, limit))

    async def _run(item: ItemT) -> T:
        async with semaphore:
            return await fn(item)

    return await asyncio.gather(
        *(_run(item) for item in items), return_exceptions=return_exceptions
    )
