"""Base Agent class for WAYR multi-agent system."""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from wayr.utils import get_logger

# Transient errors worth retrying (provider-specific imports are deferred to avoid
# import errors when a provider SDK isn't installed)
_TRANSIENT_ERRORS: tuple = (ConnectionError, TimeoutError)


def _get_transient_errors() -> tuple:
    """Build the full tuple of retryable exception types, including provider-specific ones."""
    errors = list(_TRANSIENT_ERRORS)
    try:
        from openai import RateLimitError, APITimeoutError, APIConnectionError

        errors.extend([RateLimitError, APITimeoutError, APIConnectionError])
    except ImportError:
        pass
    try:
        from anthropic import RateLimitError as AnthropicRateLimit

        errors.append(AnthropicRateLimit)
    except ImportError:
        pass
    try:
        # Gemini 5xx (503 UNAVAILABLE is common under load). 4xx are handled
        # by _is_google_rate_limit so a 400 is not retried three times.
        from google.genai.errors import ServerError as GoogleServerError

        errors.append(GoogleServerError)
    except ImportError:
        pass
    return tuple(errors)


def _is_google_rate_limit(exc: BaseException) -> bool:
    """Gemini reports rate limiting as ClientError with code 429."""
    try:
        from google.genai.errors import ClientError as GoogleClientError
    except ImportError:
        return False
    return isinstance(exc, GoogleClientError) and getattr(exc, "code", None) == 429


logger = get_logger(__name__)

# Ceiling on a single provider call. Without it a stuck connection blocks a
# whole concurrent batch forever (seen in E2E: a Gemini call hung after 503s and
# the run never finished). A timeout raises TimeoutError, which is in the retry
# set, so a stuck call is retried and then surfaced — never silently hung.
DEFAULT_CALL_TIMEOUT_SECONDS = 90.0

# Approximate per-1M-token pricing (input_cost, output_cost) in USD
_MODEL_PRICING = {
    # Google Gemini (per 1M tokens: input, output)
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.5-flash-lite": (0.05, 0.30),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-flash-lite-preview": (0.25, 1.50),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-3.5-turbo": (0.50, 1.50),
    "gpt-5.4-nano": (0.10, 0.40),
    # Anthropic
    "claude-3-haiku-20240307": (0.25, 1.25),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}

# Fallback pricing by provider when model not in table
_PROVIDER_DEFAULT_PRICING = {
    "openai": (1.00, 3.00),
    "google": (0.15, 0.60),
    "anthropic": (1.00, 5.00),
}


@dataclass
class UsageStats:
    """Tracks cumulative token usage across LLM calls for a single agent."""

    input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens (billed as output)
    reasoning_tokens: int = 0  # the hidden part of output_tokens, when reported
    cached_input_tokens: int = 0  # the part of input_tokens served from cache
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


class BaseAgent:
    """Base class for all agents in the WAYR system.

    Provides unified LLM provider handling, structured output parsing,
    and retry logic with exponential backoff.
    """

    def __init__(
        self,
        name: str,
        provider: str,
        model: str,
        api_key: str,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
    ):
        self.name = name
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.call_timeout = call_timeout
        self.usage = UsageStats()

        # Initialize appropriate client based on provider
        self.client: Any
        if provider == "openai":
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(api_key=api_key)
        elif provider == "google":
            from google import genai

            self.client = genai.Client(api_key=api_key)
        elif provider == "anthropic":
            from anthropic import AsyncAnthropic

            self.client = AsyncAnthropic(api_key=api_key)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    # Five attempts with jittered exponential backoff up to 60s. Rate limits
    # are per-minute token budgets: with ten chains in flight, a 1–2s retry
    # lands in the same exhausted window as the failure, so the waits must be
    # able to reach the next one. The jitter keeps the retries from
    # re-synchronising into another burst.
    @retry(
        wait=wait_random_exponential(multiplier=1, max=60),
        stop=stop_after_attempt(5),
        reraise=True,  # surface the provider's own error, not tenacity's wrapper
        retry=(
            retry_if_exception_type(_get_transient_errors())
            | retry_if_exception(_is_google_rate_limit)
        ),
        before_sleep=lambda retry_state: logger.warning(
            f"Retryable error (attempt {retry_state.attempt_number}/5): "
            f"{retry_state.outcome.exception() if retry_state.outcome else 'unknown'}. Retrying..."
        ),
    )
    async def _call_llm(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_output: bool = False,
    ) -> str:
        """Call LLM with unified provider handling and retry.

        Args:
            user_prompt: The user message/prompt
            system_prompt: Optional system message
            temperature: Override default temperature
            max_tokens: Override default max_tokens
            json_output: Request JSON output format (used by Google)

        Returns:
            Raw response text from the LLM
        """
        temp = temperature if temperature is not None else self.temperature
        tokens = max_tokens if max_tokens is not None else self.max_tokens

        if self.provider == "openai":
            call = self._call_openai(
                user_prompt, system_prompt, temp, tokens, json_output
            )
        elif self.provider == "google":
            call = self._call_google(user_prompt, temp, tokens, json_output)
        elif self.provider == "anthropic":
            call = self._call_anthropic(user_prompt, system_prompt, temp, tokens)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

        try:
            return await asyncio.wait_for(call, timeout=self.call_timeout)
        except asyncio.TimeoutError as e:
            # Re-raise as the builtin so the retry predicate (TimeoutError) matches
            # on every Python version, and the log says what happened.
            raise TimeoutError(
                f"[{self.name}] {self.provider}:{self.model} call exceeded "
                f"{self.call_timeout:.0f}s"
            ) from e

    async def _call_openai(
        self,
        user_prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
        json_output: bool = False,
    ) -> str:
        """Call OpenAI API with GPT-5 compatibility."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        params: Dict[str, Any] = {"model": self.model, "messages": messages}

        # GPT-5 and GPT-6 families use max_completion_tokens instead of
        # max_tokens and reject temperature/top_p while reasoning effort is
        # active, so temperature is omitted entirely (the API default of 1
        # applies — CLASSIFICATION_TEMPERATURE does not reach these models).
        if self.model.startswith(("gpt-5", "gpt-6")):
            params["max_completion_tokens"] = max_tokens
            # Match workbench defaults; reasoning_effort defaults to "none" otherwise
            params["reasoning_effort"] = "medium"
            params["verbosity"] = "medium"
        else:
            params["max_tokens"] = max_tokens
            params["temperature"] = temperature

        if json_output:
            params["response_format"] = {"type": "json_object"}

        response = await self.client.chat.completions.create(**params)
        if response.usage:
            u = response.usage
            self.usage.input_tokens += u.prompt_tokens
            self.usage.output_tokens += u.completion_tokens
            self.usage.calls += 1
            details = getattr(u, "completion_tokens_details", None)
            self.usage.reasoning_tokens += getattr(details, "reasoning_tokens", 0) or 0
            pdetails = getattr(u, "prompt_tokens_details", None)
            self.usage.cached_input_tokens += getattr(pdetails, "cached_tokens", 0) or 0
            logger.debug(
                f"[{self.name}] tokens: {u.prompt_tokens} in / {u.completion_tokens} out"
            )
        return response.choices[0].message.content

    async def _call_google(
        self,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        json_output: bool = False,
    ) -> str:
        """Call Google Gemini API (sync SDK wrapped in executor)."""
        from google.genai import types

        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_prompt)],
            ),
        ]

        config_kwargs: Dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if json_output:
            config_kwargs["response_mime_type"] = "application/json"

        config = types.GenerateContentConfig(**config_kwargs)

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            ),
        )

        if hasattr(response, "usage_metadata") and response.usage_metadata:
            um = response.usage_metadata
            inp = getattr(um, "prompt_token_count", 0) or 0
            out = getattr(um, "candidates_token_count", 0) or 0
            self.usage.input_tokens += inp
            self.usage.output_tokens += out
            self.usage.calls += 1
            logger.debug(f"[{self.name}] tokens: {inp} in / {out} out")
        return response.text

    async def _call_anthropic(
        self,
        user_prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call Anthropic Claude API."""
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = await self.client.messages.create(**kwargs)
        if response.usage:
            self.usage.input_tokens += response.usage.input_tokens
            self.usage.output_tokens += response.usage.output_tokens
            self.usage.calls += 1
            logger.debug(
                f"[{self.name}] tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out"
            )
        return response.content[0].text

    def get_usage_summary(self) -> dict:
        """Return usage stats and estimated cost for this agent."""
        pricing = _MODEL_PRICING.get(
            self.model,
            _PROVIDER_DEFAULT_PRICING.get(self.provider, (1.00, 5.00)),
        )
        input_cost = (self.usage.input_tokens / 1_000_000) * pricing[0]
        output_cost = (self.usage.output_tokens / 1_000_000) * pricing[1]
        return {
            "agent": self.name,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "total_tokens": self.usage.total_tokens,
            "calls": self.usage.calls,
            "estimated_cost_usd": round(input_cost + output_cost, 6),
        }

    def load_prompt(self, name: str, **vars) -> str:
        """Load a prompt template from wayr/prompts/ and substitute {{VAR}} placeholders.

        Args:
            name: Prompt file basename (without .md extension).
            **vars: Template variables to substitute (e.g. INDUSTRY="Technology").

        Returns:
            Prompt string with all {{VAR}} placeholders replaced.
        """
        path = Path(__file__).parent.parent / "prompts" / f"{name}.md"
        text = path.read_text(encoding="utf-8")
        for key, value in vars.items():
            text = text.replace(f"{{{{{key}}}}}", str(value))
        return text

    def _extract_tag(self, response: str, tag: str) -> Optional[str]:
        """Extract content between <tag>...</tag>, DOTALL. Returns None if not found."""
        match = re.search(
            rf"<{tag}>\s*(.*?)\s*</{tag}>", response, re.DOTALL | re.IGNORECASE
        )
        return match.group(1).strip() if match else None

    def _parse_json_response(self, response_text: str) -> dict:
        """Parse JSON from LLM response, with fallback regex extraction.

        Args:
            response_text: Raw LLM response

        Returns:
            Parsed dict

        Raises:
            json.JSONDecodeError: If no valid JSON found
        """
        # Try direct parse first
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            pass

        # Fallback: extract JSON object from surrounding text
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            return json.loads(json_match.group())

        raise json.JSONDecodeError("No valid JSON found in response", response_text, 0)
