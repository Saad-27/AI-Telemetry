"""The ingest wire format, schema version 1 (brief §6.4, §19.2, ADR 0003, ADR 0016).

Strict: unknown fields, wrong JSON types and values outside the enums, patterns or ranges are
rejected, so nothing content-like or oversized can be stored (P1, brief §11.5). It must accept
everything sdk/python/src/gaslit/_model.py and _hosts.py send, and old SDKs stay installed for
years: change a rule only together with a new schema_version.
"""

import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Self, get_args
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic_core import PydanticCustomError

Provider = Literal[
    "openai", "anthropic", "google", "mistral", "groq", "deepseek", "openrouter",
    "together", "fireworks", "xai", "meta", "amazon", "custom", "local",
]  # fmt: skip
Route = Literal[
    "direct", "azure", "bedrock", "vertex", "gemini_api", "openai_compatible", "custom", "local"
]
Operation = Literal[
    "chat.completions", "responses", "messages", "embeddings", "generate_content", "converse",
    "manual",
]  # fmt: skip
ErrorClass = Literal[
    "rate_limited", "quota_error", "auth_error", "client_error", "overloaded", "server_error",
    "timeout", "connection_error", "unknown",
]  # fmt: skip
ERROR_CLASSES: tuple[str, ...] = get_args(ErrorClass)  # one err_<class> column each
InputBucket = Literal["xs", "s", "l", "unknown"]

MAX_TOKENS = 10_000_000
MAX_COUNT = 10_000_000  # calls in one summary; the columns are 32-bit
MAX_MS = 3_600_000
TTFT_TOLERANCE_MS = 1.0  # the SDK rounds ttft and duration separately
PAST_MS = 24 * 3_600_000
FUTURE_MS = 5 * 60_000
LATENCY_BUCKETS = 20
DECODE_BUCKETS = 14

# Hosts the SDK may report (ADR 0009). Azure arrives with the customer's subdomain stripped.
_HOSTS = frozenset(
    {
        "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
        "api.groq.com", "api.mistral.ai", "api.deepseek.com", "openrouter.ai",
        "api.together.xyz", "api.together.ai", "api.fireworks.ai", "api.x.ai",
        "openai.azure.com", "services.ai.azure.com", "cognitiveservices.azure.com",
        "aiplatform.googleapis.com",
    }
)  # fmt: skip
_HOST_PATTERN = re.compile(
    r"bedrock-runtime(?:-fips)?\.[a-z]{2}(?:-[a-z]+)+-\d{1,2}\.amazonaws\.com"
    r"|[a-z]+(?:-[a-z]+)+\d{1,2}-aiplatform\.googleapis\.com"
    r"|aiplatform\.(?:us|eu)\.rep\.googleapis\.com"
)


def _allowed_host(host: str) -> str:
    if host in _HOSTS or _HOST_PATTERN.fullmatch(host):
        return host
    raise PydanticCustomError("host_not_allowed", "endpoint_host is not on the allowlist")


def _recent(ms: int, info: ValidationInfo) -> int:
    now: int = (info.context or {})["now_ms"]  # always set by validate_records
    if not now - PAST_MS <= ms <= now + FUTURE_MS:
        raise PydanticCustomError("out_of_window", "must be within the last 24 h")
    return ms


def _whole_minute(ms: int) -> int:
    if ms % 60_000:
        raise PydanticCustomError("not_a_minute", "must be the start of a minute")
    return ms


# JSON has no UUID type, so ids are the one place a string is converted.
Id = Annotated[UUID, Field(strict=False)]
EpochMs = Annotated[int, AfterValidator(_recent)]
Ms = Annotated[float, Field(ge=0, le=MAX_MS)]
Tokens = Annotated[int, Field(ge=0, le=MAX_TOKENS)]
Count = Annotated[int, Field(ge=0, le=MAX_COUNT)]
Model = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/@+-]{1,128}$")]
Tag = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
ErrorCode = Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]
Version = Annotated[str, Field(pattern=r"^[A-Za-z0-9._+-]{1,32}$")]
Region = Annotated[str, Field(pattern=r"^[a-z0-9-]{1,32}$")]
Host = Annotated[str, Field(max_length=253), AfterValidator(_allowed_host)]
LatencyHist = Annotated[list[Count], Field(min_length=LATENCY_BUCKETS, max_length=LATENCY_BUCKETS)]
DecodeHist = Annotated[list[Count], Field(min_length=DECODE_BUCKETS, max_length=DECODE_BUCKETS)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class Sample(_Strict):
    """One call sent individually (brief §6.4)."""

    event_id: Id
    reason: Literal["error", "first_of_series", "random"]
    ts: EpochMs
    duration_ms: Ms
    ttft_ms: Ms | None
    provider: Provider
    route: Route
    endpoint_host: Host | None
    endpoint_region: Region | None
    operation: Operation
    model_requested: Model
    model_returned: Model | None
    streaming: bool
    has_tools: bool
    input_tokens: Tokens | None
    output_tokens: Tokens | None
    cached_input_tokens: Tokens | None
    output_tokens_estimated: bool
    status: Literal["ok", "error", "cancelled"]
    error_class: ErrorClass | Literal["cancelled"] | None
    http_status: Annotated[int, Field(ge=100, le=599)] | None
    provider_error_code: ErrorCode | None
    service: Tag | None
    environment: Tag | None
    label: Tag | None
    origin_region: Tag | None
    provider_sdk: Tag | None
    provider_sdk_version: Version | None

    @model_validator(mode="after")
    def _ttft_within_duration(self) -> Self:
        if self.ttft_ms is not None and self.ttft_ms > self.duration_ms + TTFT_TOLERANCE_MS:
            raise PydanticCustomError("ttft_after_end", "ttft_ms is after the call ended")
        return self

    @property
    def input_bucket(self) -> InputBucket:
        """Derived on the server for samples; the SDK's rule (brief §6.5)."""
        tokens = self.input_tokens
        if tokens is None:
            return "unknown"
        return "xs" if tokens < 1_000 else "s" if tokens < 10_000 else "l"


class Series(_Strict):
    provider: Provider
    route: Route
    endpoint_host: Host | None
    endpoint_region: Region | None
    operation: Operation
    model: Model
    streaming: bool
    input_bucket: InputBucket
    service: Tag | None
    environment: Tag | None
    origin_region: Tag | None


class Summary(_Strict):
    """One series' tally for one minute from one process (brief §19.2)."""

    summary_id: Id
    minute: Annotated[EpochMs, AfterValidator(_whole_minute)]
    series: Series
    count: Annotated[int, Field(ge=1, le=MAX_COUNT)]
    ok: Count
    cancelled: Count
    errors: dict[ErrorClass, Count]
    ttft_hist: LatencyHist
    duration_hist: LatencyHist
    decode_hist: DecodeHist
    input_tokens_sum: Annotated[int, Field(ge=0)]
    output_tokens_sum: Annotated[int, Field(ge=0)]
    output_tokens_estimated_count: Count

    @model_validator(mode="after")
    def _counts_add_up(self) -> Self:
        # ADR 0003: duration counts successful calls only, decode a subset of them.
        if (
            self.ok + self.cancelled + sum(self.errors.values()) != self.count
            or sum(self.duration_hist) != self.ok
            or sum(self.ttft_hist) > self.count
            or sum(self.decode_hist) > self.ok
            or self.output_tokens_estimated_count > self.count
            or self.input_tokens_sum > self.count * MAX_TOKENS
            or self.output_tokens_sum > self.count * MAX_TOKENS
        ):
            raise PydanticCustomError("counts_mismatch", "the counts do not add up")
        return self


class _Software(_Strict):
    name: Tag
    version: Version


class _Runtime(_Strict):
    language: Tag
    version: Version


class Envelope(_Strict):
    """The batch. Records are checked one by one afterwards, so one bad record costs only itself."""

    schema_version: Literal[1]
    sdk: _Software
    runtime: _Runtime
    sent_at: Annotated[int, Field(ge=0)]
    dropped_since_last: Annotated[int, Field(ge=0)]
    summaries: list[Any]
    samples: list[Any]


MAX_ERRORS = 20  # listed in the response; the rest are only counted

# Field names are ours; anything else in an error's location (an unknown key, a dict key)
# came from the client and is never echoed.
_FIELDS = (
    frozenset(Sample.model_fields)
    | frozenset(Summary.model_fields)
    | frozenset(Series.model_fields)
)


@dataclass(slots=True)
class Records:
    summaries: list[Summary] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)
    rejected: int = 0
    errors: list[dict[str, object]] = field(default_factory=list)


def _reason(exc: ValidationError) -> str:
    """The first error as `field: type`, e.g. `ttft_ms: less_than_equal`. Never the input."""
    error = exc.errors(include_url=False, include_context=False, include_input=False)[0]
    path = []
    for part in error["loc"]:
        if not (isinstance(part, str) and part in _FIELDS):
            break
        path.append(part)
    return f"{'.'.join(path)}: {error['type']}" if path else error["type"]


def _check[M: _Strict](
    model: type[M], array: str, raws: list[Any], now_ms: int, out: Records
) -> list[M]:
    valid: list[M] = []
    for index, raw in enumerate(raws):
        try:
            valid.append(model.model_validate(raw, context={"now_ms": now_ms}))
        except ValidationError as exc:
            out.rejected += 1
            if len(out.errors) < MAX_ERRORS:
                out.errors.append({"array": array, "index": index, "reason": _reason(exc)})
    return valid


def validate_records(envelope: Envelope, now_ms: int) -> Records:
    out = Records()
    out.summaries = _check(Summary, "summaries", envelope.summaries, now_ms, out)
    out.samples = _check(Sample, "samples", envelope.samples, now_ms, out)
    return out
