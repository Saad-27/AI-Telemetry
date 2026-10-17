"""POST /v1/ingest without a database: limits, auth, parsing, responses and logs (brief §9.2,
§11.5). The real inserts are in test_ingest_db.py."""

import gzip
import json
import logging
import uuid
import zlib
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from records import envelope, gzipped, sample, summary

from app.core.config import Settings
from app.ingest.keys import IngestKey, generate
from app.ingest.routes import MAX_BODY, MAX_DECOMPRESSED, MAX_RECORDS
from app.ingest.schema import Sample, Summary
from app.main import create_app

KEY = generate()
PROJECT = uuid.uuid4()
SENTINEL = "SENTINEL-secret-text"


class FakeKeys:
    def __init__(self) -> None:
        self.error: Exception | None = None

    async def resolve(self, key: str) -> IngestKey | None:
        if self.error:
            raise self.error
        return IngestKey(uuid.uuid4(), PROJECT) if key == KEY.key else None


class RecordingSink:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.writes: list[tuple[uuid.UUID, list[Summary], list[Sample]]] = []

    async def write(
        self, project_id: uuid.UUID, summaries: Sequence[Summary], samples: Sequence[Sample]
    ) -> None:
        if self.error:
            raise self.error
        self.writes.append((project_id, list(summaries), list(samples)))


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(Settings(database_url="postgresql://nobody:x@127.0.0.1:1/none"))
    with TestClient(app) as client:
        app.state.keys = FakeKeys()
        app.state.sink = RecordingSink()
        yield client


def keys(client: TestClient) -> FakeKeys:
    return client.app.state.keys  # type: ignore[attr-defined,no-any-return]


def sink(client: TestClient) -> RecordingSink:
    return client.app.state.sink  # type: ignore[attr-defined,no-any-return]


def post(
    client: TestClient,
    body: bytes | dict[str, Any],
    key: str | None = KEY.key,
    **headers: str,
) -> Response:
    if isinstance(body, dict):
        body = gzipped(body)
        headers.setdefault("Content-Encoding", "gzip")
    headers.setdefault("Content-Type", "application/json")
    if key is not None:
        headers.setdefault("Authorization", f"Bearer {key}")
    return client.post("/v1/ingest", content=body, headers=headers)


def test_accepts_a_batch(client: TestClient) -> None:
    resp = post(client, envelope([summary()], [sample(), sample()]))
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 3, "rejected": 0, "errors": []}
    [(project, summaries, samples)] = sink(client).writes
    assert project == PROJECT and len(summaries) == 1 and len(samples) == 2


def test_uncompressed_body_is_accepted(client: TestClient) -> None:
    body = json.dumps(envelope(samples=[sample()])).encode()
    assert post(client, body).status_code == 202


def test_bad_records_are_rejected_individually(client: TestClient) -> None:
    resp = post(client, envelope(samples=[sample(), sample(provider="acme"), sample()]))
    assert resp.status_code == 202
    assert resp.json() == {
        "accepted": 2,
        "rejected": 1,
        "errors": [{"array": "samples", "index": 1, "reason": "provider: literal_error"}],
    }


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", f"Basic {KEY.key}", f"Bearer  {KEY.key}", f"Bearer {generate().key}"],
)
def test_bad_keys_get_401(client: TestClient, authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    resp = post(client, envelope(), key=None, **headers)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert resp.json() == {"error": "invalid_key"}
    assert sink(client).writes == []


def test_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    resp = post(client, envelope(), key=None, Authorization=f"bearer {KEY.key}")
    assert resp.status_code == 202


def test_database_errors_give_503_never_401(client: TestClient) -> None:
    keys(client).error = TimeoutError()
    resp = post(client, envelope())
    assert (resp.status_code, resp.headers["Retry-After"]) == (503, "5")
    keys(client).error = None
    sink(client).error = ConnectionResetError()
    assert post(client, envelope(samples=[sample()])).status_code == 503


def test_size_limits(client: TestClient) -> None:
    assert post(client, b"x" * (MAX_BODY + 1)).status_code == 413
    # No Content-Length (chunked): the cap applies while reading.
    chunks = (b"x" * 100_000 for _ in range(11))
    resp = client.post(
        "/v1/ingest",
        content=chunks,
        headers={"Authorization": f"Bearer {KEY.key}", "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_zip_bomb_is_never_fully_inflated(client: TestClient) -> None:
    bomb = gzip.compress(b" " * (MAX_DECOMPRESSED + 1))
    assert len(bomb) < 10_000
    assert post(client, bomb, **{"Content-Encoding": "gzip"}).status_code == 413
    padded = json.dumps(envelope()).encode() + b" " * (MAX_DECOMPRESSED - 300)
    assert post(client, gzip.compress(padded), **{"Content-Encoding": "gzip"}).status_code == 202


@pytest.mark.parametrize(
    "body",
    [
        b"not gzip",
        gzipped(envelope())[:-8],  # truncated
        gzipped(envelope()) + b"more",
        zlib.compress(b"{}"),  # zlib, not gzip
    ],
)
def test_bad_gzip(client: TestClient, body: bytes) -> None:
    resp = post(client, body, **{"Content-Encoding": "gzip"})
    assert (resp.status_code, resp.json()) == (400, {"error": "bad_gzip"})


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"{", "malformed_json"),
        (b'{"a": NaN}', "malformed_json"),
        (b"9" * 5000, "malformed_json"),
        (b"[]", "invalid_envelope"),
        (json.dumps({**envelope(), "schema_version": 2}).encode(), "invalid_envelope"),
        (json.dumps({**envelope(), "extra": 1}).encode(), "invalid_envelope"),
        (json.dumps({**envelope(), "summaries": {}}).encode(), "invalid_envelope"),
    ],
)
def test_malformed_envelopes_get_400(client: TestClient, body: bytes, code: str) -> None:
    resp = post(client, body)
    assert (resp.status_code, resp.json()) == (400, {"error": code})


def test_too_many_records_lets_the_sdk_split(client: TestClient) -> None:
    resp = post(client, envelope(samples=[sample()] * (MAX_RECORDS + 1)))
    assert (resp.status_code, resp.json()) == (413, {"error": "too_many_records"})


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"Content-Type": "text/plain"}, "unsupported_media_type"),
        ({"Content-Encoding": "br"}, "unsupported_encoding"),
    ],
)
def test_unsupported_types_get_415(client: TestClient, headers: dict[str, str], code: str) -> None:
    resp = post(client, envelope(), **headers)
    assert (resp.status_code, resp.json()) == (415, {"error": code})


def test_no_cors(client: TestClient) -> None:
    origin = {"Origin": "https://evil.example"}
    assert "access-control-allow-origin" not in post(client, envelope(), **origin).headers
    preflight = client.options(
        "/v1/ingest", headers={**origin, "Access-Control-Request-Method": "POST"}
    )
    assert preflight.status_code == 405
    assert "access-control-allow-origin" not in preflight.headers


def test_logs_and_responses_never_hold_keys_or_content(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = envelope(
        [summary(errors={SENTINEL: 2})],
        [sample(model_requested=f"{SENTINEL} x"), sample(**{SENTINEL: 1}), sample()],
    )
    bodies = [
        post(client, leaky).text,
        post(client, json.dumps({**envelope(), SENTINEL: 1}).encode()).text,
        post(client, f'{{"{SENTINEL}"'.encode()).text,
        post(client, envelope(), key=f"gl_live_{SENTINEL}").text,
    ]
    keys(client).error = RuntimeError(f"lookup failed for {SENTINEL} {KEY.key}")
    bodies.append(post(client, envelope()).text)
    keys(client).error = None
    sink(client).error = RuntimeError(f'duplicate key value: "{SENTINEL}"')
    bodies.append(post(client, leaky).text)

    assert "ingest 202 project=" in caplog.text
    assert "database unavailable (RuntimeError)" in caplog.text
    for text in [caplog.text, *bodies]:
        assert SENTINEL not in text
        assert KEY.key not in text
