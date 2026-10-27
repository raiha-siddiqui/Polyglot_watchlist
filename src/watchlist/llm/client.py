"""Provider-neutral client protocol, offline fake/replay clients and an opt-in Anthropic client.

No client here sends telemetry. The Anthropic SDK is imported lazily, only when
an approved live call is actually made, so ``import watchlist`` stays light.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import time
import threading
from typing import Protocol, runtime_checkable

from watchlist.llm.schemas import (
    LLMError,
    LLMRequest,
    LLMResponse,
    canonical,
    sha256_text,
)

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "llm.json"
RECEIPT_SCHEMA = "llm-receipt-v1"
_TASKS = ("parse", "explain", "judge")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_EVIDENCE_KINDS = ("live_provider", "replayed_receipt", "fake_fixture")
# Fixed allowance for message framing tokens on top of the rendered text bound.
INPUT_TOKEN_OVERHEAD = 64


@runtime_checkable
class LLMClient(Protocol):
    """Anything that turns one rendered request into raw text plus accounting."""

    provider: str
    model: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


@dataclass(frozen=True)
class Approval:
    provider_approved: bool
    data_transfer_approved: bool
    retention_approved: bool
    budget_usd: float | None

    @property
    def complete(self) -> bool:
        return (
            self.provider_approved
            and self.data_transfer_approved
            and self.retention_approved
            and self.budget_usd is not None
            and self.budget_usd > 0
        )


@dataclass(frozen=True)
class LLMConfig:
    """Validated ``config/llm.json``. Defaults keep every live path disabled."""

    provider: str
    model: str
    effort: str
    api_key_env: str
    timeout_seconds: float
    max_retries: int
    max_requests_per_run: int
    max_input_chars: int
    max_request_chars: int
    max_output_chars: int
    max_output_tokens: Mapping[str, int]
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    live_enabled: bool
    approval: Approval
    sha256: str

    def live_blockers(self) -> list[str]:
        """Every reason a live call is refused; empty means configuration allows it."""
        blockers = []
        if not self.live_enabled:
            blockers.append("live_enabled is false")
        if not self.approval.provider_approved:
            blockers.append("provider not approved")
        if not self.approval.data_transfer_approved:
            blockers.append("data transfer not approved")
        if not self.approval.retention_approved:
            blockers.append("retention not approved")
        if self.approval.budget_usd is None or self.approval.budget_usd <= 0:
            blockers.append("no positive monetary budget")
        if self.max_requests_per_run < 1:
            blockers.append("max_requests_per_run is zero")
        return blockers

    def estimate_cost(
        self, input_tokens: int | None, output_tokens: int | None
    ) -> float | None:
        if input_tokens is None or output_tokens is None:
            return None
        return round(
            (
                input_tokens * self.input_usd_per_mtok
                + output_tokens * self.output_usd_per_mtok
            )
            / 1e6,
            8,
        )

    def output_token_cap(self, request: LLMRequest) -> int:
        return min(request.max_output_tokens, self.max_output_tokens[request.task])

    def worst_case_cost(self, request: LLMRequest) -> float:
        """Upper-bound cost of one call before it is made.

        Input tokens are bounded by the UTF-8 byte length of the rendered text
        (no tokenizer emits more tokens than bytes) plus a fixed framing
        allowance; output tokens by the request's capped ``max_tokens``.
        """
        input_bound = (
            len((request.system + request.user).encode("utf-8")) + INPUT_TOKEN_OVERHEAD
        )
        return self.estimate_cost(input_bound, self.output_token_cap(request))


def _positive_int(value: object, label: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _finite(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite nonnegative number")
    return float(value)


def load_config(path: Path | None = None) -> LLMConfig:
    """Read and strictly validate the LLM configuration file."""
    path = path or DEFAULT_CONFIG
    raw_text = path.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    if not isinstance(data, dict) or data.get("schema_version") != "llm-config-v1":
        raise ValueError("unsupported LLM config schema")
    if data.get("provider") != "anthropic":
        raise ValueError("only the anthropic provider is implemented")
    model = data.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a nonblank string")
    if data.get("effort") not in _EFFORTS:
        raise ValueError(f"effort must be one of {_EFFORTS}")
    key_env = data.get("api_key_env")
    if not isinstance(key_env, str) or not key_env.isidentifier():
        raise ValueError("api_key_env must name an environment variable")
    tokens = data.get("max_output_tokens")
    if not isinstance(tokens, dict) or set(tokens) != set(_TASKS):
        raise ValueError(f"max_output_tokens must define exactly {_TASKS}")
    prices = data.get("pricing_usd_per_mtok") or {}
    approval = data.get("approval") or {}
    flags = [
        approval.get(k)
        for k in ("provider_approved", "data_transfer_approved", "retention_approved")
    ]
    if (
        any(type(flag) is not bool for flag in flags)
        or type(data.get("live_enabled")) is not bool
    ):
        raise ValueError("approval flags and live_enabled must be booleans")
    budget = approval.get("budget_usd")
    return LLMConfig(
        provider="anthropic",
        model=model,
        effort=data["effort"],
        api_key_env=key_env,
        timeout_seconds=_finite(data.get("timeout_seconds"), "timeout_seconds"),
        max_retries=_positive_int(data.get("max_retries"), "max_retries", 0),
        max_requests_per_run=_positive_int(
            data.get("max_requests_per_run"), "max_requests_per_run", 0
        ),
        max_input_chars=_positive_int(data.get("max_input_chars"), "max_input_chars"),
        max_request_chars=_positive_int(
            data.get("max_request_chars"), "max_request_chars"
        ),
        max_output_chars=_positive_int(
            data.get("max_output_chars"), "max_output_chars"
        ),
        max_output_tokens={
            k: _positive_int(tokens[k], f"max_output_tokens.{k}") for k in _TASKS
        },
        input_usd_per_mtok=_finite(prices.get("input"), "pricing input"),
        output_usd_per_mtok=_finite(prices.get("output"), "pricing output"),
        live_enabled=data["live_enabled"],
        approval=Approval(
            *flags, None if budget is None else _finite(budget, "budget_usd")
        ),
        sha256=sha256_text(raw_text),
    )


class FakeClient:
    """Scripted offline client for tests; its outputs are fixtures, never results.

    ``responses`` is either a callable of the request or a mapping keyed by
    task name. Calls are recorded so tests can assert what was sent.
    """

    provider = "fake"

    def __init__(
        self,
        responses: Callable[[LLMRequest], str] | Mapping[str, str],
        model: str = "fake-model",
    ):
        self.responses = responses
        self.model = model
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if callable(self.responses):
            text = self.responses(request)
        elif request.task in self.responses:
            text = self.responses[request.task]
        else:
            raise LLMError(
                "fake_missing", f"no scripted response for task {request.task}"
            )
        if isinstance(text, BaseException):
            raise text
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            request_sha256=request.digest(),
            stop_reason="end_turn",
            evidence_kind="fake_fixture",
        )


def make_receipt(request: LLMRequest, response: LLMResponse, recorded_at: str) -> dict:
    """Immutable receipt for a completed call. It never contains credentials."""
    return {
        "schema_version": RECEIPT_SCHEMA,
        "task": request.task,
        "prompt_id": request.prompt_id,
        "prompt_sha256": request.prompt_sha256,
        "recorded_at": recorded_at,
        "response_text": response.text,
        **response.accounting(),
    }


def _optional_count(value: object) -> bool:
    return value is None or (type(value) is int and value >= 0)


def _optional_measure(value: object) -> bool:
    return value is None or (
        type(value) in (int, float) and math.isfinite(value) and value >= 0
    )


def check_receipt(receipt: object) -> dict:
    """Schema and type checks for one receipt; raises ``invalid_receipt``.

    The response hash is an integrity check against accidental edits only. A
    plain SHA-256 is not a signature: anyone who edits a receipt can recompute
    it, so receipts prove nothing about their origin. Pin the receipts file
    hash recorded at live-run time (``receipts_sha256`` in the report) when
    provenance matters.
    """
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise LLMError("invalid_receipt", "receipt schema mismatch")
    text = receipt.get("response_text")
    if not isinstance(text, str) or sha256_text(text) != receipt.get("response_sha256"):
        raise LLMError("invalid_receipt", "response hash mismatch")
    for key in (
        "request_sha256",
        "prompt_sha256",
        "provider",
        "model",
        "task",
        "prompt_id",
    ):
        if not isinstance(receipt.get(key), str):
            raise LLMError("invalid_receipt", f"{key} must be a string")
    if receipt.get("evidence_kind") not in _EVIDENCE_KINDS:
        raise LLMError("invalid_receipt", "unknown evidence_kind")
    if not (
        receipt.get("stop_reason") is None
        or isinstance(receipt.get("stop_reason"), str)
    ):
        raise LLMError("invalid_receipt", "stop_reason must be a string or null")
    if not all(
        _optional_count(receipt.get(k)) for k in ("input_tokens", "output_tokens")
    ):
        raise LLMError(
            "invalid_receipt", "token counts must be nonnegative integers or null"
        )
    if not all(_optional_measure(receipt.get(k)) for k in ("latency_ms", "cost_usd")):
        raise LLMError(
            "invalid_receipt",
            "latency and cost must be finite nonnegative numbers or null",
        )
    return receipt


class ReplayClient:
    """Return previously recorded responses keyed by the exact request digest.

    Every receipt is type-checked and its response hash rechecked, which
    catches accidental edits but does not authenticate origin (see
    ``check_receipt``). Missing receipts raise ``replay_miss``; nothing is guessed.
    """

    provider = "replay"

    def __init__(self, receipts: Iterable[dict], model: str | None = None):
        self.by_request: dict[str, dict] = {}
        models = set()
        for receipt in receipts:
            key = check_receipt(receipt)["request_sha256"]
            if key in self.by_request:
                raise LLMError("invalid_receipt", "missing or duplicate request hash")
            self.by_request[key] = receipt
            if receipt.get("evidence_kind") == "live_provider":
                models.add(receipt.get("model"))
        # Provider receipts must come from the configured model; fixtures are labelled separately.
        if model is not None and models - {model}:
            raise LLMError(
                "invalid_receipt", "receipts were recorded with a different model"
            )
        self.model = model or (
            sorted(map(str, models))[0] if len(models) == 1 else "mixed"
        )
        self.calls: list[LLMRequest] = []

    @classmethod
    def from_file(cls, path: Path, model: str | None = None) -> "ReplayClient":
        """Load receipts from a JSON list or a JSON Lines file."""
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            receipts = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            receipts = json.loads(text)
        if not isinstance(receipts, list):
            raise LLMError("invalid_receipt", "receipt file must contain a list")
        return cls(receipts, model)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        receipt = self.by_request.get(request.digest())
        if receipt is None:
            raise LLMError("replay_miss", "no recorded response for this exact request")
        if (receipt["prompt_sha256"], receipt["task"], receipt["prompt_id"]) != (
            request.prompt_sha256,
            request.task,
            request.prompt_id,
        ):
            raise LLMError("replay_mismatch", "recorded prompt version or task differs")
        return LLMResponse(
            text=receipt["response_text"],
            provider=receipt["provider"],
            model=receipt["model"],
            request_sha256=request.digest(),
            stop_reason=receipt.get("stop_reason"),
            input_tokens=receipt.get("input_tokens"),
            output_tokens=receipt.get("output_tokens"),
            latency_ms=receipt.get("latency_ms"),
            cost_usd=receipt.get("cost_usd"),
            evidence_kind="replayed_receipt"
            if receipt.get("evidence_kind") == "live_provider"
            else "fake_fixture",
        )


class AnthropicClient:
    """Opt-in live client using the official ``anthropic`` SDK.

    Refuses to construct unless the configuration's approvals are complete and
    the caller passes ``allow_live=True``. The key is read only from the
    configured environment variable at call time and is never stored on the
    object, logged or written to receipts. No tools and no streaming are used.
    """

    provider = "anthropic"

    def __init__(self, config: LLMConfig, *, allow_live: bool = False):
        blockers = config.live_blockers()
        if not allow_live:
            blockers.insert(0, "allow_live flag not passed")
        if blockers:
            raise LLMError("live_blocked", "; ".join(blockers))
        self.config = config
        self.model = config.model
        self.requests_made = 0
        self.spent_usd = 0.0
        self._sdk_client = None
        self._call_lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            f"AnthropicClient(model={self.model!r}, requests_made={self.requests_made})"
        )

    def _client(self):
        if self._sdk_client is None:
            key = os.environ.get(self.config.api_key_env)
            if not key:
                raise LLMError(
                    "missing_api_key",
                    f"environment variable {self.config.api_key_env} is not set",
                )
            import anthropic  # Lazy: optional dependency, imported only for approved live calls.

            self._sdk_client = anthropic.Anthropic(
                api_key=key,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
            )
        return self._sdk_client

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Serialize calls so request/cost reservations cannot race across threads."""
        with self._call_lock:
            return self._complete(request)

    def _complete(self, request: LLMRequest) -> LLMResponse:
        """One bounded call; reserve the request and cost before sending."""
        if request.size_chars() > self.config.max_request_chars:
            raise LLMError(
                "request_too_large",
                f"rendered request exceeds {self.config.max_request_chars} characters",
            )
        if self.requests_made >= self.config.max_requests_per_run:
            raise LLMError("budget_exhausted", "request cap reached")
        budget = self.config.approval.budget_usd or 0.0
        worst = self.config.worst_case_cost(request)
        if self.spent_usd + worst > budget:
            raise LLMError(
                "budget_exhausted",
                "worst-case cost of this call would exceed the monetary budget",
            )
        client = self._client()
        import anthropic

        self.requests_made += 1
        # Charge the worst case up front; it is replaced by the measured cost if usage is reported.
        self.spent_usd += worst
        started = time.perf_counter()
        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=self.config.output_token_cap(request),
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                output_config={"effort": self.config.effort},
            )
        except anthropic.APIStatusError as exc:
            raise LLMError("provider_status", f"HTTP {exc.status_code}") from None
        except anthropic.APIConnectionError:
            raise LLMError(
                "provider_connection", "connection or timeout failure"
            ) from None
        latency = round((time.perf_counter() - started) * 1000, 3)
        text = "".join(
            block.text
            for block in message.content
            if getattr(block, "type", None) == "text"
        )
        usage = getattr(message, "usage", None)
        input_tokens, output_tokens = (
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )
        if not (_optional_count(input_tokens) and _optional_count(output_tokens)):
            input_tokens = output_tokens = None
        cost = self.config.estimate_cost(input_tokens, output_tokens)
        if cost is not None:
            # Reported usage replaces the reservation; it is never charged below what was reported.
            self.spent_usd += cost - worst
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=str(message.model),
            request_sha256=request.digest(),
            stop_reason=message.stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
            cost_usd=cost,
            evidence_kind="live_provider",
        )


class RecordingClient:
    """Wrap a live client and append each provider receipt to disk as soon as it returns.

    Paid responses are therefore kept even if a later case or report step
    fails. Lines are flushed and fsynced; the file is opened in append mode
    and never truncated.
    """

    def __init__(
        self, inner: LLMClient, path: Path, clock: Callable[[], str] | None = None
    ):
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.path = path
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())
        self.recorded = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = self.inner.complete(request)
        if (
            isinstance(response, LLMResponse)
            and response.evidence_kind == "live_provider"
        ):
            line = canonical(make_receipt(request, response, self.clock())) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self.recorded += 1
        return response


def call_model(
    client: LLMClient, request: LLMRequest
) -> tuple[LLMResponse | None, str | None]:
    """Call a client and convert every failure into a recorded reason string.

    Client exceptions are untrusted too: only the class name and a stable code
    are kept, never the exception text, which could echo prompt or key material.
    """
    try:
        response = client.complete(request)
    except LLMError as exc:
        return None, f"client_error:{exc.code}"
    except Exception as exc:  # noqa: BLE001 - any client failure must fall back.
        return None, f"client_error:{type(exc).__name__}"
    if not isinstance(response, LLMResponse) or not isinstance(response.text, str):
        return None, "client_error:invalid_response_object"
    if response.stop_reason not in (None, "end_turn", "stop_sequence"):
        return response, f"stop_reason:{response.stop_reason}"
    return response, None


__all__ = [
    "LLMClient",
    "LLMConfig",
    "FakeClient",
    "ReplayClient",
    "RecordingClient",
    "AnthropicClient",
    "load_config",
    "check_receipt",
    "make_receipt",
    "call_model",
    "canonical",
]
