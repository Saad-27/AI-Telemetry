"""POST /v1/ingest (brief §9.2, §11.5, ADR 0016).

Cheapest checks first: headers, then whether the machine is keeping up (ADR 0025), then the key
(format and checksum before any database work; a key not cached waits while its IP has sent
too many invalid ones, ADR 0028), then the quota and rate limit (from memory, ADR 0017), then
the body under hard size caps, then validation, then one insert per record type.
Logs never include bodies, keys, headers or driver error text: only codes, counts and types.
Every answer is counted in the metrics by status and reason code (ADR 0021).
"""

import logging
import math
import random
import time
import zlib

import pydantic_core
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from app.core.entitlements import entitlements
from app.core.metrics import BATCH_RECORDS, INSERT_SECONDS, RECORDS, REQUEST_SECONDS, REQUESTS
from app.ingest.schema import Envelope, validate_records

log = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY = 1_000_000  # bytes as sent (gzipped or not)
MAX_DECOMPRESSED = 2_000_000  # the SDK's largest batch, 1,000 records, is about 0.7 MB
# A rejected body is charged to the rate limit at one token per this many bytes read or inflated,
# about a record's worth, so payloads only a hand-made client sends (a zip bomb costs ~5 ms of
# CPU) use up the sender's allowance rather than the machine (ADR 0025).
REJECTED_BYTES_PER_TOKEN = 1_000
# Requests between the shed check and their answer. Past this the machine is behind, so new
# requests are turned away at once rather than queued until their clients give up, which made
# overloaded ingest store almost nothing (docs/capacity.md). 429, not 503: after a 429 the SDK
# keeps the batch and waits Retry-After; after three 5xx it drops it (ADR 0025).
MAX_IN_FLIGHT = 50
SHED_RETRY_AFTER_S = (3, 10)  # chosen at random, so shed SDKs do not all come back at once
MAX_RECORDS = 1_000  # summaries plus samples; over it, 413 makes the SDK split the batch
RETRY_AFTER_S = "5"
# Over quota, SDKs check back this often rather than at the reset, so an account that upgrades
# resumes sending at its next check once the upgrade flow calls Quota.unblock. Each check is
# answered from memory, with no key lookup (ADR 0019).
QUOTA_RETRY_AFTER_S = 60
# Bound once: looking a labelled child up costs about a microsecond each time.
_RECORDS = {
    (kind, outcome): RECORDS.labels(kind, outcome)
    for kind in ("summary", "sample")
    for outcome in ("accepted", "rejected")
}


@router.post("/v1/ingest")
async def ingest(request: Request) -> JSONResponse:
    started = time.perf_counter()
    headers = request.headers
    length = headers.get("content-length")
    if length is not None and not (
        length.isascii() and length.isdigit() and int(length) <= MAX_BODY
    ):
        return _reply(started, 413, "too_large")
    if headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        return _reply(started, 415, "unsupported_media_type")
    encoding = headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("gzip", "identity"):
        return _reply(started, 415, "unsupported_encoding")

    state = request.app.state
    if state.in_flight >= MAX_IN_FLIGHT:
        wait = random.randint(*SHED_RETRY_AFTER_S)  # noqa: S311 (spreading retries)
        return _reply(started, 429, "overloaded", {"Retry-After": str(wait)})
    state.in_flight += 1
    try:
        return await _ingest(request, encoding, started)
    finally:
        state.in_flight -= 1


async def _ingest(request: Request, encoding: str, started: float) -> JSONResponse:
    state = request.app.state
    key = _bearer(request.headers.get("authorization"))
    found = state.keys.cached(key) if key else None
    if found is None:
        # Behind Cloudflare this is CF-Connecting-IP, set by EdgeOnly (ADR 0028).
        ip = request.client.host if request.client else ""
        if wait := state.invalid_keys.wait_s(ip):
            return _reply(started, 429, "too_many_invalid_keys", {"Retry-After": _seconds(wait)})
        try:
            found = await state.keys.resolve(key) if key else None
        except Exception as exc:
            return _unavailable(started, exc)
        if found is None:
            state.invalid_keys.charge(ip)
            return _reply(started, 401, "invalid_key", {"WWW-Authenticate": "Bearer"})
    limits = entitlements(found.plan)
    if wait := state.quota.wait_s(found.user_id, limits):
        wait = min(wait, QUOTA_RETRY_AFTER_S)
        return _reply(started, 429, "quota_exceeded", {"Retry-After": _seconds(wait)})
    if wait := state.rate.wait_s(found.user_id, limits):
        return _reply(started, 429, "rate_limited", {"Retry-After": _seconds(wait)})

    def rejected(status: int, code: str, size: int) -> JSONResponse:
        state.rate.charge(found.user_id, math.ceil(size / REJECTED_BYTES_PER_TOKEN))
        return _reply(started, status, code)

    try:
        body = await _read(request, MAX_BODY)
    except ClientDisconnect:
        # The client gave up mid-body (an SDK timing out under load): nobody hears the answer,
        # so one log line and a metric instead of a traceback. 499 as in nginx's logs.
        return _reply(started, 499, "disconnected")
    if body is None:
        return rejected(413, "too_large", MAX_BODY)
    if encoding == "gzip":
        try:
            inflated = _gunzip(body, MAX_DECOMPRESSED)
        except (zlib.error, ValueError):
            # How much was inflated before the fault is unknown: charged as the most.
            return rejected(400, "bad_gzip", MAX_DECOMPRESSED)
        if inflated is None:
            return rejected(413, "too_large", MAX_DECOMPRESSED)
        body = inflated
    try:
        data = pydantic_core.from_json(body, allow_inf_nan=False)
    except ValueError:
        return rejected(400, "malformed_json", len(body))
    try:
        envelope = Envelope.model_validate(data)
    except ValidationError:
        return rejected(400, "invalid_envelope", len(body))
    sent = len(envelope.summaries) + len(envelope.samples)
    if sent > MAX_RECORDS:
        return rejected(413, "too_many_records", len(body))
    state.rate.charge(found.user_id, sent)

    records = validate_records(envelope, now_ms=time.time_ns() // 1_000_000)
    try:
        with INSERT_SECONDS.time():
            await state.sink.write(found.project_id, records.summaries, records.samples)
    except Exception as exc:
        return _unavailable(started, exc)
    # Counted only once stored, like the response, so a retried batch is counted once.
    BATCH_RECORDS.observe(sent)
    for kind, got, valid in (
        ("summary", envelope.summaries, records.summaries),
        ("sample", envelope.samples, records.samples),
    ):
        if valid:
            _RECORDS[kind, "accepted"].inc(len(valid))
        if len(got) > len(valid):
            _RECORDS[kind, "rejected"].inc(len(got) - len(valid))
    accepted = len(records.summaries) + len(records.samples)
    state.quota.add(found.user_id, sum(s.count for s in records.summaries), accepted)
    log.info(
        "ingest 202 project=%s accepted=%d rejected=%d ms=%.1f",
        found.project_id,
        accepted,
        records.rejected,
        _done(started, 202, "accepted"),
    )
    return JSONResponse(
        {"accepted": accepted, "rejected": records.rejected, "errors": records.errors},
        status_code=202,
    )


def _bearer(value: str | None) -> str | None:
    scheme, _, token = (value or "").partition(" ")
    return token if scheme.lower() == "bearer" else None


async def _read(request: Request, limit: int) -> bytes | None:
    """The body, or None as soon as it passes `limit` (also catches a missing Content-Length)."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _gunzip(data: bytes, limit: int) -> bytes | None:
    """One gzip member, inflated to at most `limit` + 1 bytes, so a zip bomb never fully
    expands. None if over the limit; raises if truncated, corrupt or followed by more data."""
    inflater = zlib.decompressobj(wbits=31)  # gzip header and trailer
    out = inflater.decompress(data, limit + 1)
    if len(out) > limit:
        return None
    if not inflater.eof or inflater.unused_data:
        raise ValueError("incomplete gzip")
    return out


def _seconds(wait: float) -> str:
    """Retry-After in whole seconds, rounded up so the client never retries early."""
    return str(max(1, math.ceil(wait)))


def _done(started: float, status: int, reason: str) -> float:
    """Counts the answer in the metrics; returns the milliseconds taken, for the log line."""
    seconds = time.perf_counter() - started
    REQUESTS.labels(str(status), reason).inc()
    REQUEST_SECONDS.observe(seconds)
    return seconds * 1000


def _reply(
    started: float, status: int, code: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    log.info("ingest %d %s ms=%.1f", status, code, _done(started, status, code))
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _unavailable(started: float, exc: Exception) -> JSONResponse:
    # Type only: driver messages can quote row values or connection details.
    log.warning("ingest: database unavailable (%s)", type(exc).__name__)
    return _reply(started, 503, "unavailable", {"Retry-After": RETRY_AFTER_S})
