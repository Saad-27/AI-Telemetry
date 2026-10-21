"""Refuse requests that skipped Cloudflare (ADR 0027, docs/security-config.md §3).

`gaslit-api-production.fly.dev` reaches the app directly, around Cloudflare's firewall and
per-IP rate limit. Cloudflare adds a secret header to every request it forwards; without it,
the app answers 503 before routing. Pure ASGI: about 2 µs per request either way.

503, not 403: a wrong secret is usually our own mistake (a rotation or a Terraform change),
and the SDK stops sending for good after a 403 but retries a 503, so users recover by
themselves once it is fixed (ADR 0027).

On requests that pass, Cloudflare's `CF-Connecting-IP` becomes the request's client address,
so per-IP limits see the real sender. Only then can it be trusted: Cloudflare overwrites the
header, but a request straight to Fly could carry any value.
"""

import hmac
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

HEADER = b"x-gaslit-edge"  # set by Terraform (infra/terraform/main.tf)
CLIENT_IP = b"cf-connecting-ip"
# Fly's health checks reach the machine directly, without the header. Liveness only: no
# database work, so open to anyone.
OPEN_PATHS = frozenset({"/healthz"})
_BODY = b'{"error":"edge_check_failed"}'
_START = {
    "type": "http.response.start",
    "status": 503,
    "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(_BODY)).encode()),
        (b"retry-after", b"5"),
    ],
}


class EdgeOnly:
    def __init__(self, app: ASGIApp, secrets: Iterable[str]) -> None:
        self.app = app
        # More than one during a rotation: the old value until Cloudflare sends the new one.
        self.secrets = tuple(s.encode() for s in secrets)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        given = client = b""
        kept = []
        for name, value in scope["headers"]:
            if name == HEADER:
                given = value
            else:
                if name == CLIENT_IP:
                    client = value
                kept.append((name, value))
        # compare_digest: timing must not reveal how much of a guess was right.
        if not any(hmac.compare_digest(given, s) for s in self.secrets):
            await send(_START)
            await send({"type": "http.response.body", "body": _BODY})
            return
        # The app never sees the secret, so no later log or error report can leak it.
        scope = {**scope, "headers": kept}
        if client:
            scope["client"] = (client.decode("latin-1"), 0)  # Cloudflare does not pass the port
        await self.app(scope, receive, send)
