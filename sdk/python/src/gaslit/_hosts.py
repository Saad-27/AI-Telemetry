"""Provider and route detection from the endpoint hostname (brief §6.6).

This is the one allowlist of hosts the SDK may report. Only the hostname is read from a URL:
the path, query, port and any credentials in it are dropped, so none can reach an event (P1).
A host that is not on the list is ``custom`` or ``local`` and is reported as ``None`` (P6).
Nothing here raises: a URL that can't be parsed is ``custom``.
"""

from __future__ import annotations

import ipaddress
import re
from functools import lru_cache
from typing import NamedTuple
from urllib.parse import urlsplit


class Endpoint(NamedTuple):
    provider: str
    route: str
    host: str | None  # sent as endpoint_host
    region: str | None  # sent as endpoint_region


CUSTOM = Endpoint("custom", "custom", None, None)
LOCAL = Endpoint("local", "local", None, None)

OPENAI = Endpoint("openai", "direct", "api.openai.com", None)
ANTHROPIC = Endpoint("anthropic", "direct", "api.anthropic.com", None)
GEMINI_API = Endpoint("google", "gemini_api", "generativelanguage.googleapis.com", None)

# Providers reached through the OpenAI client's base_url: the OpenAI-compatible route.
_COMPATIBLE = {
    "api.groq.com": "groq",
    "api.mistral.ai": "mistral",
    "api.deepseek.com": "deepseek",
    "openrouter.ai": "openrouter",
    "api.together.xyz": "together",
    "api.together.ai": "together",
    "api.fireworks.ai": "fireworks",
    "api.x.ai": "xai",
}
_FIXED = {
    "api.openai.com": OPENAI,
    "api.anthropic.com": ANTHROPIC,
    "generativelanguage.googleapis.com": GEMINI_API,
    **{h: Endpoint(p, "openai_compatible", h, None) for h, p in _COMPATIBLE.items()},
}

# Azure puts the customer's resource name in the subdomain, so only the suffix is reported.
_AZURE_SUFFIXES = ("openai.azure.com", "services.ai.azure.com", "cognitiveservices.azure.com")

_AWS_REGION = r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}"  # us-east-1, us-gov-west-1
_GCP_REGION = r"[a-z]+(?:-[a-z]+)+\d{1,2}"  # us-central1, europe-west4
_BEDROCK = re.compile(rf"bedrock-runtime(?:-fips)?\.({_AWS_REGION})\.amazonaws\.com").fullmatch
_VERTEX = re.compile(rf"({_GCP_REGION})-aiplatform\.googleapis\.com").fullmatch
_VERTEX_MULTI = re.compile(r"aiplatform\.(us|eu)\.rep\.googleapis\.com").fullmatch  # "us", "eu"

# Bedrock model IDs start with the vendor ("anthropic.claude-..."), after an optional
# cross-Region prefix ("us.anthropic.claude-...") or ARN path.
_BEDROCK_VENDORS = {
    "anthropic": "anthropic", "meta": "meta", "amazon": "amazon", "mistral": "mistral",
    "deepseek": "deepseek", "openai": "openai", "google": "google",
}  # fmt: skip
# Vertex model names by family. Anything else on Vertex is Google's own.
_VERTEX_FAMILIES = (
    ("claude", "anthropic"), ("meta/", "meta"), ("llama", "meta"), ("mistral", "mistral"),
    ("codestral", "mistral"), ("deepseek", "deepseek"), ("openai/", "openai"),
)  # fmt: skip

_LOCAL_SUFFIXES = (".local", ".internal", ".localhost")


@lru_cache(maxsize=64)
def _by_host(host: str) -> Endpoint:
    """The endpoint for a host. Bedrock and Vertex get their provider from the model later."""
    if known := _FIXED.get(host):
        return known
    if any(host.endswith("." + s) for s in _AZURE_SUFFIXES):
        suffix = next(s for s in _AZURE_SUFFIXES if host.endswith("." + s))
        return Endpoint("openai", "azure", suffix, None)
    if m := _BEDROCK(host):
        return Endpoint("custom", "bedrock", host, m.group(1))
    if host == "aiplatform.googleapis.com":
        return Endpoint("google", "vertex", host, "global")
    if m := _VERTEX(host) or _VERTEX_MULTI(host):
        return Endpoint("google", "vertex", host, m.group(1))
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return LOCAL
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return CUSTOM
    # Loopback, private, link-local and shared (CGNAT, e.g. Tailscale) addresses.
    return CUSTOM if ip.is_global else LOCAL


@lru_cache(maxsize=64)
def _hostname(text: str) -> str | None:
    text = text.strip()
    if "://" not in text:
        text = "//" + text
    host = urlsplit(text).hostname  # drops credentials, port, path and query
    return host.rstrip(".").lower() if host else None


def _bedrock_provider(model: str) -> str:
    parts = model.rsplit("/", 1)[-1].split(".")
    for vendor in parts[:2]:
        if vendor in _BEDROCK_VENDORS:
            return _BEDROCK_VENDORS[vendor]
    return "custom"


def _vertex_provider(model: str) -> str:
    name = model.lower().rsplit("publishers/", 1)[-1]
    for prefix, provider in _VERTEX_FAMILIES:
        if name.startswith(prefix):
            return provider
    return "google"


def detect(base_url: object, model: object = None) -> Endpoint:
    """Classify an endpoint from its base URL. ``model`` is only read to pick the vendor on
    Bedrock and Vertex, and is never stored."""
    try:
        host = _hostname(str(base_url))
        if host is None:
            return CUSTOM
        found = _by_host(host)
        if found.route == "bedrock":
            vendor = _bedrock_provider(model) if isinstance(model, str) else "custom"
            return found._replace(provider=vendor)
        if found.route == "vertex" and isinstance(model, str):
            return found._replace(provider=_vertex_provider(model))
        return found
    except Exception:  # fail open: an odd URL must never break a call
        return CUSTOM
