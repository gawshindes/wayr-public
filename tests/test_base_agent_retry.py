"""BaseAgent retries transient provider errors — including Gemini's."""

from unittest.mock import AsyncMock

import pytest
from google.genai.errors import ClientError, ServerError
from tenacity import wait_none

from wayr.agents.base import BaseAgent, _get_transient_errors, _is_google_rate_limit


class _Err:
    """Build google.genai errors without a real httpx response."""

    @staticmethod
    def server(code=503):
        e = ServerError.__new__(ServerError)
        Exception.__init__(e, f"{code} UNAVAILABLE")
        e.code = code
        return e

    @staticmethod
    def client(code):
        e = ClientError.__new__(ClientError)
        Exception.__init__(e, f"{code}")
        e.code = code
        return e


def agent() -> BaseAgent:
    a = BaseAgent(name="t", provider="google", model="gemini-flash-latest", api_key="g")
    a._call_llm.retry.wait = wait_none()  # don't sleep in tests
    return a


class TestRetrySet:
    def test_gemini_server_error_is_transient(self):
        assert ServerError in _get_transient_errors()

    def test_only_429_client_errors_are_rate_limits(self):
        assert _is_google_rate_limit(_Err.client(429))
        assert not _is_google_rate_limit(_Err.client(400))
        assert not _is_google_rate_limit(RuntimeError("x"))


class TestRetryBehaviour:
    @pytest.mark.asyncio
    async def test_503_twice_then_success(self):
        a = agent()
        a._call_google = AsyncMock(
            side_effect=[_Err.server(503), _Err.server(503), "ok"]
        )
        assert await a._call_llm("p") == "ok"
        assert a._call_google.await_count == 3

    @pytest.mark.asyncio
    async def test_429_is_retried(self):
        a = agent()
        a._call_google = AsyncMock(side_effect=[_Err.client(429), "ok"])
        assert await a._call_llm("p") == "ok"
        assert a._call_google.await_count == 2

    @pytest.mark.asyncio
    async def test_400_is_not_retried(self):
        a = agent()
        a._call_google = AsyncMock(side_effect=_Err.client(400))
        with pytest.raises(ClientError):
            await a._call_llm("p")
        assert a._call_google.await_count == 1

    @pytest.mark.asyncio
    async def test_gives_up_after_five_attempts(self):
        a = agent()
        a._call_google = AsyncMock(side_effect=_Err.server(503))
        with pytest.raises(ServerError):
            await a._call_llm("p")
        assert a._call_google.await_count == 5


class TestCallTimeout:
    @pytest.mark.asyncio
    async def test_hung_call_times_out_is_retried_then_surfaces(self):
        import asyncio

        a = BaseAgent(
            name="t", provider="google", model="m", api_key="g", call_timeout=0.05
        )
        a._call_llm.retry.wait = wait_none()

        async def hang(*_a, **_k):
            await asyncio.sleep(10)

        a._call_google = hang
        with pytest.raises(TimeoutError, match="exceeded 0s"):
            await a._call_llm("p")

    @pytest.mark.asyncio
    async def test_slow_then_fast_succeeds_via_retry(self):
        import asyncio

        a = BaseAgent(
            name="t", provider="google", model="m", api_key="g", call_timeout=0.05
        )
        a._call_llm.retry.wait = wait_none()
        calls = {"n": 0}

        async def slow_once(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(10)
            return "ok"

        a._call_google = slow_once
        assert await a._call_llm("p") == "ok"
        assert calls["n"] == 2

    def test_default_timeout_is_sane(self):
        from wayr.agents.base import DEFAULT_CALL_TIMEOUT_SECONDS

        assert 30 <= DEFAULT_CALL_TIMEOUT_SECONDS <= 180
