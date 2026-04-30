# Implementation Plan: LLM Client Abstraction

## Spec Context

This plan implements the thin LLM provider abstraction used by every part of the application that calls an LLM — summarization, analysis, query, and lint. It fulfills the FR-3.4 retry/backoff requirement (up to 5 attempts, max 30-second delay with jitter), the FR-2 provider routing requirement (Anthropic primary, OpenAI optional, both selected by config), and the inter-request delay enforcement described in FR-3.4. The Anthropic SDK is the default and primary path; the OpenAI path is a thin alternative that uses the same interface.

Catalog item: 3 — LLM Client Abstraction
Specification section: FR-2 (provider/model from config), FR-3.4 (retry with exponential backoff, non-retriable error exit, inter-request delay), Technical Context (Anthropic SDK primary, OpenAI optional, `tenacity` for retry)
Acceptance criteria addressed: FR-3.4 retry (5 attempts, initial 1 s, doubling, max 30 s, jitter), FR-3.4 non-retriable error (log + exit 1), FR-3.4 inter-request delay, FR-2 provider/model config, Technical Context (Anthropic native SDK as primary)

## Dependencies

- **Blocked by**: Item 1 (Project Scaffold) — package must exist; `tenacity`, `anthropic`, `openai` declared in `pyproject.toml`
- **Blocked by**: Item 2 (Configuration Model) — `WikiConfig` provides `llm_provider`, `llm_model`, `inter_request_delay`
- **Blocks**: Item 5 (Summarizer), Item 6 (Analysis), Item 7 (Query Core), Item 8 (Lint Staleness)
- **Uses**: `anthropic` Python SDK, `openai` Python SDK, `tenacity` (retry), `time` (stdlib), `logging` (stdlib), `os` (stdlib)

## File Changes

### New Files

- `codebase_wiki_builder/llm_client.py` — `LLMClient` class with `complete(prompt: str) -> str`, provider routing, retry logic, inter-request delay

### Modified Files

- None

## Implementation Details

### `LLMClient` Class

**File**: `codebase_wiki_builder/llm_client.py`

**Exports**:
- `LLMClient` — main class; constructed once per run, shared across all callers
- `LLMError` — exception raised for non-retriable API errors (callers catch this to exit with code 1)

**Backoff constants** (module-level, not in config — hardcoded per spec):

```python
RETRY_MAX_ATTEMPTS = 5
RETRY_INITIAL_WAIT = 1.0    # seconds
RETRY_MAX_WAIT = 30.0       # seconds
RETRY_MULTIPLIER = 2.0      # doubles each attempt
```

**Class interface**:

```python
class LLMClient:
    def __init__(self, config: WikiConfig) -> None:
        ...

    def complete(self, prompt: str) -> str:
        """Send prompt to the configured LLM provider and return the response text.

        Enforces inter-request delay before each call.
        Retries on HTTP 429 (rate limit) with exponential backoff + jitter.
        Raises LLMError on non-retriable errors.
        """
        ...
```

### Constructor

`__init__` reads `config.llm_provider` and `config.llm_model` to select and initialize the appropriate SDK client. API keys are read from environment variables (populated by `load_dotenv()` in `config.py`).

```python
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
    else:
        # Should never reach here — config validation rejects unknown providers
        raise LLMError(f"Unknown LLM provider: {self._provider!r}")
```

SDK clients are stored as instance attributes. Imports are deferred inside `__init__` (not at module top level) to make the provider conditional: if a user only has `ANTHROPIC_API_KEY` set and uses the default provider, no OpenAI import failure can occur.

### `complete()` Method — Inter-Request Delay

Before every API call, enforce the configured delay since the last call. Use `time.monotonic()` for timing.

```python
def _enforce_delay(self) -> None:
    """Sleep if less than inter_request_delay seconds have elapsed since last call."""
    if self._inter_request_delay <= 0:
        return
    elapsed = time.monotonic() - self._last_call_time
    remaining = self._inter_request_delay - elapsed
    if remaining > 0:
        time.sleep(remaining)
```

`_enforce_delay()` is called at the start of `complete()`, before the retry loop begins. This means the delay is enforced once per `complete()` call, not per retry attempt (retries themselves use `tenacity`'s wait mechanism).

### `complete()` Method — Retry Logic

Use `tenacity` for retry. The retry decorator wraps an inner `_call_api()` method that performs the raw SDK call. `complete()` calls `_enforce_delay()`, updates `_last_call_time`, then calls the tenacity-wrapped `_call_api()`.

The `tenacity` `retry` decorator is applied to `_call_api()` with:
- `stop=stop_after_attempt(RETRY_MAX_ATTEMPTS)` — 5 total attempts
- `wait=wait_exponential(multiplier=RETRY_MULTIPLIER, min=RETRY_INITIAL_WAIT, max=RETRY_MAX_WAIT) + wait_random(0, 1)` — jitter up to 1 extra second
- `retry=retry_if_exception_type(RateLimitError)` — only retry on rate-limit errors
- `reraise=True` — after 5 failures, re-raise the last exception rather than wrapping it
- `before_sleep=before_sleep_log(logger, logging.WARNING)` — log each retry attempt at WARNING level

Where `RateLimitError` is the provider-specific rate-limit exception class (Anthropic: `anthropic.RateLimitError`; OpenAI: `openai.RateLimitError`).

```python
def complete(self, prompt: str) -> str:
    self._enforce_delay()
    self._last_call_time = time.monotonic()
    try:
        return self._call_api(prompt)
    except Exception as exc:
        # After retries exhausted, or non-retriable error — wrap in LLMError
        raise LLMError(f"LLM API call failed: {exc}") from exc
```

### `_call_api()` — Provider Dispatch

`_call_api()` is a separate method so that `tenacity` can be applied cleanly. It dispatches to the appropriate provider method.

```python
def _call_api(self, prompt: str) -> str:
    if self._provider == "anthropic":
        return self._call_anthropic(prompt)
    else:
        return self._call_openai(prompt)
```

Because `tenacity` retry is applied to `_call_api`, retries are transparent to `complete()`. The rate-limit exception type checked in `retry_if_exception_type` must be the correct SDK class for the active provider.

**Implementation note**: Since both providers can be configured, and `tenacity`'s `retry_if_exception_type` is static, use a union retry condition:

```python
from tenacity import retry_if_exception_type

def _is_rate_limit(exc: BaseException) -> bool:
    """Return True if exc is a rate-limit error from any supported provider."""
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
```

Apply `retry_if_exception(_is_rate_limit)` rather than `retry_if_exception_type(...)` so the predicate works for both providers regardless of which SDK is active.

### Anthropic Provider Call

```python
def _call_anthropic(self, prompt: str) -> str:
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
```

`max_tokens=8192` is a practical cap on output length. Summaries, analyses, and query answers are all well within this limit. If a future use case requires longer output, this can be made configurable — for MVP it is hardcoded.

### OpenAI Provider Call

```python
def _call_openai(self, prompt: str) -> str:
    import openai
    try:
        response = self._openai_client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8192,
        )
        return response.choices[0].message.content or ""
    except openai.RateLimitError:
        raise  # Let tenacity handle retry
    except openai.APIError as exc:
        raise LLMError(f"OpenAI API error (non-retriable): {exc}") from exc
```

### `LLMError` Exception

```python
class LLMError(Exception):
    """Raised for non-retriable LLM API errors or missing API keys.

    Callers (CLI commands) catch this, log the message, and call sys.exit(1).
    """
```

`LLMError` is a plain `Exception` subclass with no additional attributes. Callers log `str(exc)` and exit. This keeps the error surface simple.

### Tenacity Decorator Application

Apply the retry decorator to `_call_api` as a method decorator at class definition time. Since `tenacity` decorators work on regular functions, apply it by wrapping in `__init__` using `tenacity.retry(...)` as a function:

```python
import tenacity

class LLMClient:
    def __init__(self, config: WikiConfig) -> None:
        ...
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
```

Then `complete()` calls `self._call_api_with_retry(prompt)` instead of `self._call_api(prompt)`. This approach avoids the class-level decorator complication with `self` and ensures each instance gets its own retry-wrapped callable.

### Module-Level Logger

```python
import logging
logger = logging.getLogger(__name__)
```

Retry attempts are logged at `WARNING` level (via `before_sleep_log`). Non-retriable errors are logged at `ERROR` level by the CLI caller (not here).

### Complete Module Skeleton

```python
from __future__ import annotations

import logging
import os
import time

import tenacity

from codebase_wiki_builder.config import WikiConfig

logger = logging.getLogger(__name__)

RETRY_MAX_ATTEMPTS = 5
RETRY_INITIAL_WAIT = 1.0
RETRY_MAX_WAIT = 30.0
RETRY_MULTIPLIER = 2.0


class LLMError(Exception):
    """Non-retriable LLM API error or missing API key."""


def _is_rate_limit(exc: BaseException) -> bool:
    """Return True if exc is a rate-limit error from any configured provider."""
    ...  # as shown above


class LLMClient:
    def __init__(self, config: WikiConfig) -> None: ...
    def _enforce_delay(self) -> None: ...
    def complete(self, prompt: str) -> str: ...
    def _call_api(self, prompt: str) -> str: ...
    def _call_anthropic(self, prompt: str) -> str: ...
    def _call_openai(self, prompt: str) -> str: ...
```

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `ANTHROPIC_API_KEY` not set, provider is `anthropic` | `LLMError` raised in `__init__`; CLI catches, logs, `sys.exit(1)` |
| `OPENAI_API_KEY` not set, provider is `openai` | `LLMError` raised in `__init__`; CLI catches, logs, `sys.exit(1)` |
| HTTP 429 rate limit — attempt < 5 | `tenacity` retries with exponential backoff + jitter; WARNING logged before each sleep |
| HTTP 429 rate limit — all 5 attempts exhausted | `tenacity` re-raises the last `RateLimitError`; `complete()` wraps in `LLMError`; CLI catches, logs, `sys.exit(1)` |
| Anthropic auth failure (`anthropic.AuthenticationError`) | Caught as `anthropic.APIError` in `_call_anthropic`; wrapped in `LLMError`; NOT retried |
| OpenAI auth failure (`openai.AuthenticationError`) | Caught as `openai.APIError` in `_call_openai`; wrapped in `LLMError`; NOT retried |
| Anthropic bad request (`anthropic.BadRequestError`) | Same as auth failure — non-retriable |
| Network timeout / connection error | These are subclasses of `APIError` in both SDKs; treated as non-retriable for MVP |
| Unknown provider string | Guard in `__init__` raises `LLMError`; should never occur after config validation |

## Unit Test Specifications

**File**: `tests/test_llm_client.py`

### `LLMClient.__init__()` — API key validation

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Anthropic, key set | `ANTHROPIC_API_KEY=sk-test` in env | `LLMClient` constructs without error | Happy path |
| Anthropic, key missing | `ANTHROPIC_API_KEY` absent | `LLMError` raised on construction | FR-2: key required |
| OpenAI, key set | `llm_provider="openai"`, `OPENAI_API_KEY=sk-test` | Constructs without error | OpenAI happy path |
| OpenAI, key missing | `llm_provider="openai"`, no `OPENAI_API_KEY` | `LLMError` raised on construction | FR-2: key required |

### `_enforce_delay()`

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| No delay configured (0) | `inter_request_delay=0` | No `time.sleep` call | FR-3.4: delay is configurable |
| Delay, first call | `inter_request_delay=1.0`, `_last_call_time=0` | `time.sleep` called with ~1.0 s | First call always waits full delay |
| Delay, recent call | `_last_call_time` set to 0.5 s ago, `inter_request_delay=1.0` | `time.sleep` called with ~0.5 s | Partial wait for elapsed time |
| Delay, stale call | `_last_call_time` set to 2 s ago, `inter_request_delay=1.0` | No sleep (elapsed > delay) | No over-waiting |

### `complete()` — happy path

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Anthropic success | Mock `_call_anthropic` → `"response text"` | Returns `"response text"` | Basic happy path |
| OpenAI success | Mock `_call_openai` → `"response text"` | Returns `"response text"` | OpenAI path works |

### `complete()` — retry behavior

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| Rate limit once, then success | Mock API: raises `RateLimitError` once, then returns response | Returns response; `_call_api` called twice | Retry on 429 |
| Rate limit twice, then success | Mock API: raises `RateLimitError` twice, then returns | Returns response; called 3 times | Multiple retries |
| Rate limit all 5 attempts | Mock API: raises `RateLimitError` 5 times | `LLMError` raised | All retries exhausted → exit path |
| Auth error — no retry | Mock API: raises `AuthenticationError` | `LLMError` raised after 1 call | Non-retriable error |
| Bad request — no retry | Mock API: raises `BadRequestError` | `LLMError` raised after 1 call | Non-retriable error |

### Backoff wait values (unit test with mock)

| Case | Setup | Expected | Why |
|------|-------|----------|-----|
| First retry wait | Intercept tenacity sleep on attempt 2 | Sleep ≥ 1.0 s and ≤ 2.0 s (with jitter) | Initial wait = 1 s |
| Second retry wait | Intercept sleep on attempt 3 | Sleep ≥ 2.0 s and ≤ 5.0 s | Doubles + jitter |
| Wait cap | Intercept sleep on attempt 5 | Sleep ≤ 31.0 s (max 30 s + up to 1 s jitter) | Max wait enforced |

### Key Scenario: 5 Rate-Limit Retries Exhausted

**Setup**: Create `LLMClient` with Anthropic config. Mock `self._anthropic_client.messages.create` to always raise `anthropic.RateLimitError`. Patch `time.sleep` to avoid actual waits.

**Action**: Call `client.complete("test prompt")`.

**Expected**:
- `LLMError` is raised (not `RateLimitError` directly).
- The underlying `_call_api` method was invoked exactly 5 times.
- A `WARNING`-level log message was emitted before each of the 4 sleep intervals.

```python
from unittest.mock import MagicMock, patch
import pytest
import anthropic

def test_rate_limit_exhausted(monkeypatch, tmp_path):
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient, LLMError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = WikiConfig(codebase_path=str(tmp_path), inter_request_delay=0)

    client = LLMClient(config)

    call_count = 0
    def mock_create(**kwargs):
        nonlocal call_count
        call_count += 1
        raise anthropic.RateLimitError(
            message="rate limited", response=MagicMock(), body={}
        )

    client._anthropic_client.messages.create = mock_create

    with patch("time.sleep"):  # skip actual waiting
        with pytest.raises(LLMError):
            client.complete("hello")

    assert call_count == 5
```

### Key Scenario: Non-Retriable Auth Error

**Setup**: Create `LLMClient`. Mock `messages.create` to raise `anthropic.AuthenticationError`.

**Action**: Call `client.complete("test")`.

**Expected**: `LLMError` raised after exactly 1 call (no retries).

```python
def test_auth_error_not_retried(monkeypatch, tmp_path):
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient, LLMError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = WikiConfig(codebase_path=str(tmp_path), inter_request_delay=0)
    client = LLMClient(config)

    call_count = 0
    def mock_create(**kwargs):
        nonlocal call_count
        call_count += 1
        raise anthropic.AuthenticationError(
            message="invalid key", response=MagicMock(), body={}
        )

    client._anthropic_client.messages.create = mock_create

    with pytest.raises(LLMError):
        client.complete("hello")

    assert call_count == 1
```

### Key Scenario: Inter-Request Delay Enforcement

**Setup**: Create `LLMClient` with `inter_request_delay=1.0`. Mock API to return successfully. Patch `time.sleep` to record calls.

**Action**: Call `client.complete("first")`, then immediately call `client.complete("second")`.

**Expected**: On the second call, `time.sleep` is called with a value close to 1.0 s (the elapsed time since the first call will be near zero, so the sleep covers the full delay).

```python
def test_inter_request_delay(monkeypatch, tmp_path):
    from codebase_wiki_builder.config import WikiConfig
    from codebase_wiki_builder.llm_client import LLMClient

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = WikiConfig(codebase_path=str(tmp_path), inter_request_delay=1.0)
    client = LLMClient(config)

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="ok")]
    client._anthropic_client.messages.create = MagicMock(return_value=mock_response)

    sleep_calls = []
    with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        client.complete("first")
        client.complete("second")

    # First call: delay enforced from epoch (last_call_time=0), full delay slept
    # Second call: just completed first, so nearly full delay slept again
    assert len(sleep_calls) >= 1
    assert sleep_calls[-1] > 0.5  # at least half the delay was waited
```

## Notes

- **Deferred SDK imports in `__init__`**: Both `anthropic` and `openai` are imported inside `__init__` rather than at module top level. This is intentional — it avoids `ImportError` noise if one SDK is somehow not installed, and keeps the import graph clean. Both SDKs are in `pyproject.toml`, so the `ImportError` case should not arise in practice.
- **`max_tokens=8192`**: Hardcoded for MVP. All outputs (summaries, analysis overviews, query answers) are expected to be well under this limit. If a future use case needs longer output, this should become a `WikiConfig` field.
- **Retry applies only to `_call_api_with_retry`**: The retry wrapper is built in `__init__`. This means `_call_api`, `_call_anthropic`, and `_call_openai` are ordinary methods that can be easily mocked in tests without fighting `tenacity`.
- **`tenacity.wait_random(0, 1)` for jitter**: Adds 0–1 second of random jitter on top of the exponential wait. This is consistent with the spec's "random jitter" requirement and is a standard pattern for avoiding thundering-herd retries when multiple instances run simultaneously.
- **`_last_call_time` initialized to 0.0**: On the very first call, `time.monotonic() - 0.0` will be a large positive number (seconds since system boot), so `elapsed > inter_request_delay` will be true and no sleep will occur. This is the correct behavior — the first call should not be delayed.
- **Non-retriable network errors**: Connection timeouts and network errors are `APIConnectionError` subclasses in both SDKs. These are treated as non-retriable for MVP because the spec only specifies retry for HTTP 429. If connection reliability becomes an issue, `_is_rate_limit` can be extended to include connection errors.
- **`LLMClient` is not thread-safe**: `_last_call_time` is a shared mutable attribute. For MVP (sequential, single-threaded processing), this is not a concern. Do not use one `LLMClient` instance across threads.
- **Caller responsibility for exit**: `LLMError` is raised rather than calling `sys.exit(1)` directly. The CLI command (catalog items 9, 10, 13) catches `LLMError`, logs the error message, and calls `sys.exit(1)`. This keeps `llm_client.py` as a reusable library module rather than a CLI-entangled one.
- **Test mocking strategy**: Tests should mock `client._anthropic_client.messages.create` (or `client._openai_client.chat.completions.create`) directly rather than patching `anthropic.Anthropic`. This is simpler and avoids patching the SDK constructor.
- **`anthropic.RateLimitError` constructor**: The Anthropic SDK's error classes require `message`, `response`, and `body` arguments. Tests that need to raise `RateLimitError` should use `MagicMock()` for `response` and `{}` for `body` to satisfy the constructor. Check SDK source if the signature changes between versions.
