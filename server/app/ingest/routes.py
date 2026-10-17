"""POST /v1/ingest (brief §9.2, §11.5, ADR 0016).

Cheapest checks first: headers, then the key (format and checksum before any database work),
then the quota and rate limit (from memory, ADR 0017), then the body under hard size caps, then
validation, then one insert per record type.
Logs never include bodies, keys, headers or driver error text: only codes, counts and types.
"""

import logging
import math
import time
import zlib

import pydantic_core
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.entitlements import entitlements
from app.ingest.schema import Envelope, validate_records

log = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY = 1_000_000  # bytes as sent (gzipped or not)
MAX_DECOMPRESSED = 5_000_000
MAX_RECORDS = 1_000  # summaries plus samples; over it, 413 makes the SDK split the batch
RETRY_AFTER_S = "5"


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

    key = _bearer(headers.get("authorization"))
    try:
        found = await request.app.state.keys.resolve(key) if key else None
    except Exception as exc:
        return _unavailable(started, exc)
    if found is None:
        return _reply(started, 401, "invalid_key", {"WWW-Authenticate": "Bearer"})
    limits = entitlements(found.plan)
    state = request.app.state
    if wait := state.quota.wait_s(found.user_id, limits):
        return _reply(started, 429, "quota_exceeded", {"Retry-After": _seconds(wait)})
    if wait := state.rate.wait_s(found.id, limits):
        return _reply(started, 429, "rate_limited", {"Retry-After": _seconds(wait)})

    body = await _read(request, MAX_BODY)
    if body is not None and encoding == "gzip":
        try:
            body = _gunzip(body, MAX_DECOMPRESSED)
        except (zlib.error, ValueError):
            return _reply(started, 400, "bad_gzip")
    if body is None:
        return _reply(started, 413, "too_large")
    try:
        data = pydantic_core.from_json(body, allow_inf_nan=False)
    except ValueError:
        return _reply(started, 400, "malformed_json")
    try:
        envelope = Envelope.model_validate(data)
    except ValidationError:
        return _reply(started, 400, "invalid_envelope")
    sent = len(envelope.summaries) + len(envelope.samples)
    if sent > MAX_RECORDS:
        return _reply(started, 413, "too_many_records")
    state.rate.charge(found.id, sent)

    records = validate_records(envelope, now_ms=time.time_ns() // 1_000_000)
    try:
        await state.sink.write(found.project_id, records.summaries, records.samples)
    except Exception as exc:
        return _unavailable(started, exc)
    state.quota.add(found.user_id, sum(s.count for s in records.summaries))
    accepted = len(records.summaries) + len(records.samples)
    log.info(
        "ingest 202 project=%s accepted=%d rejected=%d ms=%.1f",
        found.project_id,
        accepted,
        records.rejected,
        _ms(started),
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


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _reply(
    started: float, status: int, code: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    log.info("ingest %d %s ms=%.1f", status, code, _ms(started))
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _unavailable(started: float, exc: Exception) -> JSONResponse:
    # Type only: driver messages can quote row values or connection details.
    log.warning("ingest: database unavailable (%s)", type(exc).__name__)
    return _reply(started, 503, "unavailable", {"Retry-After": RETRY_AFTER_S})
