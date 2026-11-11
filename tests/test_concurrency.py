"""Tests for the bounded-concurrency fan-out helper."""

import asyncio

import pytest

from wayr.utils import map_concurrent


class TestOrdering:
    """Results must line up with inputs — callers zip the two together."""

    @pytest.mark.asyncio
    async def test_results_follow_input_order_not_completion_order(self):
        # Later items finish first, so completion order is the reverse of input.
        async def slow_for_small(n: int) -> int:
            await asyncio.sleep((10 - n) / 1000)
            return n * 2

        result = await map_concurrent(list(range(10)), slow_for_small, limit=10)

        assert result == [n * 2 for n in range(10)]

    @pytest.mark.asyncio
    async def test_empty_input(self):
        async def boom(item):  # pragma: no cover - must never run
            raise AssertionError("should not be called")

        assert await map_concurrent([], boom, limit=5) == []


class TestConcurrencyLimit:
    """`limit` must actually bound simultaneous in-flight calls."""

    @pytest.mark.asyncio
    async def test_never_exceeds_limit(self):
        in_flight = 0
        peak = 0

        async def track(_item):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

        await map_concurrent(list(range(20)), track, limit=4)

        assert peak <= 4

    @pytest.mark.asyncio
    async def test_limit_below_one_is_clamped(self):
        async def identity(n: int) -> int:
            return n

        assert await map_concurrent([1, 2, 3], identity, limit=0) == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_actually_runs_concurrently(self):
        """20 items x 10ms serially is 200ms; at limit=10 it should be ~20ms."""

        async def sleeper(_item):
            await asyncio.sleep(0.01)

        loop = asyncio.get_running_loop()
        start = loop.time()
        await map_concurrent(list(range(20)), sleeper, limit=10)
        elapsed = loop.time() - start

        assert elapsed < 0.1


class TestExceptionHandling:
    """A failing item must not silently vanish or take the batch down."""

    @pytest.mark.asyncio
    async def test_raises_by_default(self):
        async def fail_on_three(n: int) -> int:
            if n == 3:
                raise ValueError("boom")
            return n

        with pytest.raises(ValueError, match="boom"):
            await map_concurrent(list(range(5)), fail_on_three, limit=2)

    @pytest.mark.asyncio
    async def test_return_exceptions_keeps_slot_alignment(self):
        async def fail_on_three(n: int) -> int:
            if n == 3:
                raise ValueError("boom")
            return n

        result = await map_concurrent(
            list(range(5)), fail_on_three, limit=2, return_exceptions=True
        )

        assert len(result) == 5
        assert isinstance(result[3], ValueError)
        assert [r for i, r in enumerate(result) if i != 3] == [0, 1, 2, 4]

    @pytest.mark.asyncio
    async def test_one_failure_does_not_block_the_rest(self):
        async def fail_on_even(n: int) -> int:
            if n % 2 == 0:
                raise RuntimeError(f"bad {n}")
            return n

        result = await map_concurrent(
            list(range(6)), fail_on_even, limit=3, return_exceptions=True
        )

        succeeded = [r for r in result if not isinstance(r, BaseException)]
        assert succeeded == [1, 3, 5]
