"""LLM client abstraction for Codebase Wiki Builder.

Provides a thin provider-agnostic wrapper around the Anthropic and OpenAI SDKs
with exponential-backoff retry on rate limits and inter-request delay enforcement.
"""

from __future__ import annotations

import logging
import os
import time

import tenacity

from codebase_wiki_builder.config import WikiConfig

logger = logging.getLogger(__name__)

RETRY_MAX_ATTEMPTS = 5
RETRY_INITIAL_WAIT = 1.0    # seconds
RETRY_MAX_WAIT = 30.0       # seconds
RETRY_MULTIPLIER = 2.0      # doubles each attempt


class LLMError(Exception):
    """Raised for non-retriable LLM API errors or missing API keys.

    Callers (CLI commands) catch this, log the message, and call sys.exit(1).
    """


def _is_rate_limit(exc: BaseException) -> bool:
    """Return True if exc is a rate-limit error from any configured provider."""
    try:
        import anthropic
        if isinstance(exc, anthropic.RateLimitError):
            return True
    except ImportError:
        pass
    try:
        import openai
        if isinstance(exc, openai.RateLimitError):
            return True
    except ImportError:
        pass
    return False


class LLMClient:
    """Thin LLM provider abstraction with retry and inter-request delay."""

    def __init__(self, config: WikiConfig) -> None:
        self._provider = config.llm_provider
        self._model = config.llm_model
        self._inter_request_delay = config.inter_request_delay
        self._last_call_time: float = 0.0  # monotonic timestamp of last API call

        if self._provider == "anthropic":
            import anthropic
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is not set. "
                    "Add it to your .env file in the vault root."
                )
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        elif self._provider == "openai":
            import openai
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise LLMError(
                    "OPENAI_API_KEY is not set. "
                    "Add it to your .env file in the vault root."
                )
            self._openai_client = openai.OpenAI(api_key=api_key)
            # o1/o3/o4/gpt-5.x series require max_completion_tokens, not max_tokens
            self._use_max_completion_tokens = self._model.startswith(("o1", "o3", "o4", "gpt-5"))
        else:
            # Should never reach here — config validation rejects unknown providers
            raise LLMError(f"Unknown LLM provider: {self._provider!r}")

        # Build the retrying wrapper once, bound to instance
        self._call_api_with_retry = tenacity.retry(
            stop=tenacity.stop_after_attempt(RETRY_MAX_ATTEMPTS),
            wait=(
                tenacity.wait_exponential(
                    multiplier=RETRY_MULTIPLIER,
                    min=RETRY_INITIAL_WAIT,
                    max=RETRY_MAX_WAIT,
                )
                + tenacity.wait_random(0, 1)
            ),
            retry=tenacity.retry_if_exception(_is_rate_limit),
            reraise=True,
            before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
        )(self._call_api)

    def _enforce_delay(self) -> None:
        """Sleep if less than inter_request_delay seconds have elapsed since last call."""
        if self._inter_request_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_call_time
        remaining = self._inter_request_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def complete(self, prompt: str) -> str:
        """Send prompt to the configured LLM provider and return the response text.

        Enforces inter-request delay before each call.
        Retries on HTTP 429 (rate limit) with exponential backoff + jitter.
        Raises LLMError on non-retriable errors.
        """
        self._enforce_delay()
        self._last_call_time = time.monotonic()
        try:
            return self._call_api_with_retry(prompt)
        except Exception as exc:
            # After retries exhausted, or non-retriable error — wrap in LLMError
            raise LLMError(f"LLM API call failed: {exc}") from exc

    def _call_api(self, prompt: str) -> str:
        """Dispatch to the appropriate provider method."""
        if self._provider == "anthropic":
            return self._call_anthropic(prompt)
        else:
            return self._call_openai(prompt)

    def _call_anthropic(self, prompt: str) -> str:
        """Call the Anthropic Messages API and return the response text."""
        import anthropic
        try:
            message = self._anthropic_client.messages.create(
                model=self._model,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except anthropic.RateLimitError:
            raise  # Let tenacity handle retry
        except anthropic.APIError as exc:
            # Non-retriable: auth failure, bad request, etc.
            raise LLMError(f"Anthropic API error (non-retriable): {exc}") from exc

    def _call_openai(self, prompt: str) -> str:
        """Call the OpenAI Chat Completions API and return the response text."""
        import openai
        kwargs: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._use_max_completion_tokens:
            kwargs["max_completion_tokens"] = 8192
        else:
            kwargs["max_tokens"] = 8192
        try:
            response = self._openai_client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except openai.RateLimitError:
            raise  # Let tenacity handle retry
        except openai.APIError as exc:
            raise LLMError(f"OpenAI API error (non-retriable): {exc}") from exc
