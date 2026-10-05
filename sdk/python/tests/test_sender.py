import gzip
import json
import logging
import os
import subprocess
import sys
import time

import pytest
from conftest import KEY
from fake_ingest import GARBAGE, FakeIngest, Reply

import yourpkg
from yourpkg import _runtime
from yourpkg._model import Call
from yourpkg._sender import RETRY_AFTER_DEFAULT_S, RETRY_AFTER_MAX_S, Sender, retry_after_s

pytestmark = pytest.mark.sender


def start(ingest: FakeIngest, **options: object) -> None:
    yourpkg.init(api_key=KEY, endpoint=ingest.url, **options)  # type: ignore[arg-type]


def calls(n: int, model: str = "m", fail: bool = False) -> None:
    for _ in range(n):
        with yourpkg.track(provider="custom", model=model) as t:
            if fail:
                t.set_error(http_status=503)


def records(env: dict[str, object]) -> int:
    return len(env["summaries"]) + len(env["samples"])  # type: ignore[arg-type]


def test_flush_sends_summary_and_sample(ingest: FakeIngest) -> None:
    start(ingest, service="svc")
    calls(3)
    yourpkg.flush()
    [req] = ingest.wait_for(1)
    assert req.status == 202
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert req.headers["Content-Encoding"] == "gzip"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["User-Agent"] == f"yourpkg-python/{yourpkg.__version__}"
    env = req.envelope
    assert env is not None
    assert env["schema_version"] == 1
    assert env["sdk"] == {"name": "yourpkg-python", "version": yourpkg.__version__}
    assert env["dropped_since_last"] == 0
    [summary] = env["summaries"]
    assert (summary["count"], summary["series"]["service"]) == (3, "svc")
    [sample] = env["samples"]
    assert sample["reason"] == "first_of_series"


def test_flush_waits_for_the_sender(ingest: FakeIngest) -> None:
    start(ingest)
    calls(1)
    yourpkg.flush()
    assert len(ingest.received) == 1  # sent before flush() returned


def test_flush_before_any_call_returns_at_once(ingest: FakeIngest) -> None:
    start(ingest)
    t0 = time.monotonic()
    yourpkg.flush()
    assert time.monotonic() - t0 < 0.1
    assert ingest.received == []


def test_closed_minutes_are_sent_without_flush(ingest: FakeIngest) -> None:
    start(ingest, flush_interval=0.1)
    call = Call(provider="custom", route="custom", operation="manual", model_requested="m")
    call.ts_ms -= 120_000  # finished two minutes ago
    _runtime.record(call)
    [req] = ingest.wait_for(1)
    assert req.envelope is not None and len(req.envelope["summaries"]) == 1


def test_full_batch_wakes_the_sender(ingest: FakeIngest) -> None:
    start(ingest, max_batch=2, flush_interval=300.0)
    calls(1, model="a")
    calls(1, model="b")  # second first_of_series sample fills the batch
    [req] = ingest.wait_for(1)
    assert req.envelope is not None and len(req.envelope["samples"]) == 2


def test_batches_respect_max_batch(ingest: FakeIngest) -> None:
    start(ingest, max_batch=2)
    for i in range(5):
        calls(1, model=f"m{i}")
    yourpkg.flush()
    sizes = [records(e) for e in ingest.accepted()]
    assert sum(sizes) == 10 and max(sizes) == 2  # 5 samples and 5 summaries


def test_retries_reuse_ids_then_succeed(ingest: FakeIngest) -> None:
    ingest.script(500, GARBAGE, 503)
    start(ingest)
    calls(1)
    yourpkg.flush()
    reqs = ingest.wait_for(4)
    assert [r.status for r in reqs] == [500, GARBAGE, 503, 202]
    bodies = [json.loads(r.body) for r in reqs]
    assert len({b["summaries"][0]["summary_id"] for b in bodies}) == 1
    assert len({b["samples"][0]["event_id"] for b in bodies}) == 1


def test_batch_dropped_after_retries_and_reported(ingest: FakeIngest) -> None:
    ingest.script(500, 500, 500, 500)
    start(ingest)
    calls(1)
    yourpkg.flush()
    calls(1)
    yourpkg.flush()
    reqs = ingest.wait_for(5)
    assert [r.status for r in reqs] == [500, 500, 500, 500, 202]
    assert ingest.accepted()[0]["dropped_since_last"] == 2  # one summary, one sample


def test_429_waits_for_retry_after(ingest: FakeIngest) -> None:
    ingest.script(Reply(429, {"Retry-After": "0.3"}))
    start(ingest)
    calls(1)
    assert not yourpkg._runtime.state.sender.flush(0.1)  # type: ignore[union-attr]
    assert len(ingest.received) == 1
    yourpkg.flush()
    assert [r.status for r in ingest.wait_for(2)] == [429, 202]


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("12", 12.0),
        ("0", 0.0),
        ("9999", RETRY_AFTER_MAX_S),
        ("-1", RETRY_AFTER_DEFAULT_S),
        ("nan", RETRY_AFTER_DEFAULT_S),
        ("Wed, 21 Oct 2026 07:28:00 GMT", RETRY_AFTER_DEFAULT_S),
        (None, RETRY_AFTER_DEFAULT_S),
    ],
)
def test_retry_after_parsing(value: str | None, seconds: float) -> None:
    assert retry_after_s(value) == seconds


def test_413_splits_once(ingest: FakeIngest) -> None:
    ingest.script(413, 413)  # whole batch, then the first half
    start(ingest)
    calls(1, model="a")
    calls(1, model="b")
    yourpkg.flush()
    reqs = ingest.wait_for(3)
    assert [r.status for r in reqs] == [413, 413, 202]
    assert [records(json.loads(r.body)) for r in reqs] == [4, 2, 2]
    assert ingest.accepted()[0]["dropped_since_last"] == 2


def test_400_drops_the_batch_and_carries_on(
    ingest: FakeIngest, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="yourpkg")
    ingest.script(400)
    start(ingest, max_batch=1)
    calls(1)
    yourpkg.flush()
    assert [r.status for r in ingest.wait_for(2)] == [400, 202]
    assert "HTTP 400" in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_stops_the_sdk(
    ingest: FakeIngest, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    caplog.set_level(logging.WARNING, logger="yourpkg")
    ingest.script(status)
    start(ingest, max_batch=1)
    calls(3)  # one batch per record: the first is rejected, the rest are never sent
    t0 = time.monotonic()
    yourpkg.flush()
    assert time.monotonic() - t0 < 1.0
    assert _runtime.state is None
    calls(1)
    yourpkg.flush()
    assert len(ingest.received) == 1
    assert caplog.text.count("Sending has stopped") == 1


def test_redirect_is_not_followed(ingest: FakeIngest) -> None:
    with FakeIngest() as elsewhere:
        ingest.script(Reply(307, {"Location": f"{elsewhere.url}/v1/ingest"}))
        start(ingest)
        calls(1)
        yourpkg.flush()
        assert elsewhere.received == []  # the key never reaches the redirect target


def test_debug_prints_exactly_what_is_sent(
    ingest: FakeIngest, capsys: pytest.CaptureFixture[str]
) -> None:
    start(ingest, debug=True)
    calls(1)
    yourpkg.flush()
    [req] = ingest.wait_for(1)
    printed = capsys.readouterr().err
    assert f"[yourpkg] payload {req.body.decode()}\n" in printed


def test_debug_without_key_prints_and_sends_nothing(
    ingest: FakeIngest, capsys: pytest.CaptureFixture[str]
) -> None:
    yourpkg.init(endpoint=ingest.url, debug=True)
    calls(1)
    yourpkg.flush()
    env = json.loads(capsys.readouterr().err.split("[yourpkg] payload ", 1)[1])
    assert len(env["summaries"]) == 1
    assert ingest.received == []


def test_shutdown_flushes_and_stops(ingest: FakeIngest) -> None:
    start(ingest)
    calls(1)
    yourpkg.shutdown()
    assert len(ingest.received) == 1
    assert _runtime.state is None
    calls(1)  # a no-op now


def test_flush_and_shutdown_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object) -> None:
        raise RuntimeError

    monkeypatch.setattr("yourpkg._runtime.flush", boom)
    monkeypatch.setattr("yourpkg._runtime.shutdown", boom)
    yourpkg.flush()
    yourpkg.shutdown()


def test_atexit_flushes_unsent_data(ingest: FakeIngest) -> None:
    script = (
        "import yourpkg\n"
        f"yourpkg.init(api_key={KEY!r}, endpoint={ingest.url!r})\n"
        "with yourpkg.track(provider='custom', model='m'):\n"
        "    pass\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)
    [req] = ingest.wait_for(1)
    assert req.envelope is not None and req.envelope["summaries"][0]["count"] == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX only")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # fork with threads running
def test_fork_child_starts_empty(ingest: FakeIngest) -> None:
    start(ingest)
    calls(1)  # parent: one call pending, sender thread running
    parent = _runtime.state
    assert parent is not None
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: report what it inherited, then exit without running atexit
        s = _runtime.state
        ok = (
            s is not None
            and s is not parent
            and s.aggregator.pop_all() == []
            and len(s.queue) == 0
            and s.sender._thread is None
        )
        calls(1, model="child")
        yourpkg.flush()
        os.write(w, b"1" if ok else b"0")
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        assert f.read() == b"1"
    os.waitpid(pid, 0)
    yourpkg.flush()
    models = sorted(s["series"]["model"] for e in ingest.accepted() for s in e["summaries"])
    assert models == ["child", "m"]  # each process sent only its own calls


def test_body_is_gzip_json(ingest: FakeIngest, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[bytes] = []
    real_post = Sender._post

    def spy(self: Sender, body: bytes) -> tuple[int | None, str | None]:
        sent.append(body)
        return real_post(self, body)

    monkeypatch.setattr(Sender, "_post", spy)
    start(ingest)
    calls(1)
    yourpkg.flush()
    assert json.loads(gzip.decompress(sent[0]))["schema_version"] == 1
