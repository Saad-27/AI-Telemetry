import pytest

from gaslit._hosts import CUSTOM, LOCAL, detect
from gaslit._model import PROVIDERS, ROUTES

CASES = [
    # url, model, provider, route, host, region
    ("https://api.openai.com/v1", None, "openai", "direct", "api.openai.com", None),
    ("https://api.anthropic.com", None, "anthropic", "direct", "api.anthropic.com", None),
    ("https://generativelanguage.googleapis.com/v1beta", None,
     "google", "gemini_api", "generativelanguage.googleapis.com", None),
    ("https://api.groq.com/openai/v1", None,
     "groq", "openai_compatible", "api.groq.com", None),
    ("https://api.mistral.ai/v1", None, "mistral", "openai_compatible", "api.mistral.ai", None),
    ("https://api.deepseek.com", None,
     "deepseek", "openai_compatible", "api.deepseek.com", None),
    ("https://openrouter.ai/api/v1", None,
     "openrouter", "openai_compatible", "openrouter.ai", None),
    ("https://api.together.xyz/v1", None,
     "together", "openai_compatible", "api.together.xyz", None),
    ("https://api.together.ai/v1", None,
     "together", "openai_compatible", "api.together.ai", None),
    ("https://api.fireworks.ai/inference/v1", None,
     "fireworks", "openai_compatible", "api.fireworks.ai", None),
    ("https://api.x.ai/v1", None, "xai", "openai_compatible", "api.x.ai", None),
    # Azure: the resource name is the customer's, so only the suffix is kept.
    ("https://acme-prod.openai.azure.com/openai/v1/", None,
     "openai", "azure", "openai.azure.com", None),
    ("https://acme.services.ai.azure.com", None,
     "openai", "azure", "services.ai.azure.com", None),
    ("https://acme.cognitiveservices.azure.com", None,
     "openai", "azure", "cognitiveservices.azure.com", None),
    # Bedrock: the provider comes from the model ID.
    ("https://bedrock-runtime.us-east-1.amazonaws.com", "anthropic.claude-3-5-sonnet-20241022-v2:0",
     "anthropic", "bedrock", "bedrock-runtime.us-east-1.amazonaws.com", "us-east-1"),
    ("https://bedrock-runtime.eu-west-1.amazonaws.com", "eu.anthropic.claude-sonnet-4-v1:0",
     "anthropic", "bedrock", "bedrock-runtime.eu-west-1.amazonaws.com", "eu-west-1"),
    ("https://bedrock-runtime-fips.us-gov-west-1.amazonaws.com", "meta.llama3-70b-instruct-v1:0",
     "meta", "bedrock", "bedrock-runtime-fips.us-gov-west-1.amazonaws.com", "us-gov-west-1"),
    ("https://bedrock-runtime.us-west-2.amazonaws.com", "amazon.nova-pro-v1:0",
     "amazon", "bedrock", "bedrock-runtime.us-west-2.amazonaws.com", "us-west-2"),
    ("https://bedrock-runtime.us-west-2.amazonaws.com",
     "arn:aws:bedrock:us-west-2:123456789012:inference-profile/us.mistral.pixtral-large-v1:0",
     "mistral", "bedrock", "bedrock-runtime.us-west-2.amazonaws.com", "us-west-2"),
    ("https://bedrock-runtime.us-west-2.amazonaws.com", "cohere.command-r-v1:0",
     "custom", "bedrock", "bedrock-runtime.us-west-2.amazonaws.com", "us-west-2"),
    ("https://bedrock-runtime.us-west-2.amazonaws.com", None,
     "custom", "bedrock", "bedrock-runtime.us-west-2.amazonaws.com", "us-west-2"),
    # Vertex: Google unless the model is a partner's.
    ("https://us-east5-aiplatform.googleapis.com/v1", "claude-sonnet-4@20250514",
     "anthropic", "vertex", "us-east5-aiplatform.googleapis.com", "us-east5"),
    ("https://europe-west4-aiplatform.googleapis.com", "gemini-2.5-pro",
     "google", "vertex", "europe-west4-aiplatform.googleapis.com", "europe-west4"),
    ("https://aiplatform.googleapis.com", "gemini-2.5-flash",
     "google", "vertex", "aiplatform.googleapis.com", "global"),
    ("https://us-central1-aiplatform.googleapis.com", "meta/llama-3.3-70b-instruct-maas",
     "meta", "vertex", "us-central1-aiplatform.googleapis.com", "us-central1"),
    ("https://us-central1-aiplatform.googleapis.com", None,
     "google", "vertex", "us-central1-aiplatform.googleapis.com", "us-central1"),
]  # fmt: skip


@pytest.mark.parametrize(("url", "model", "provider", "route", "host", "region"), CASES)
def test_known_hosts(
    url: str, model: str | None, provider: str, route: str, host: str, region: str | None
) -> None:
    assert detect(url, model) == (provider, route, host, region)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1", "http://LOCALHOST:8000", "http://127.0.0.1:8080",
        "http://127.1.2.3", "http://[::1]:8000/v1", "http://10.0.0.5", "http://172.16.4.1:9",
        "http://192.168.1.20/v1", "http://169.254.169.254", "http://100.101.102.103:8000",
        "http://ollama.local:11434", "http://vllm.svc.cluster.internal/v1", "http://a.localhost",
    ],
)  # fmt: skip
def test_local(url: str) -> None:
    assert detect(url) == LOCAL


@pytest.mark.parametrize(
    "url",
    [
        "https://my-gateway.example.com/v1", "https://8.8.8.8/v1", "https://vllm/v1",
        "", "   ", "not a url", "https://", "http://[bad", "https://exa mple.com",
    ],
)  # fmt: skip
def test_custom_reports_no_host(url: str) -> None:
    assert detect(url) == CUSTOM


@pytest.mark.parametrize(
    "url",
    [  # lookalikes of allowlisted hosts must not be trusted
        "https://api.openai.com.evil.example/v1", "https://evil-api.openai.com",
        "https://notapi.anthropic.com", "https://openai.azure.com", "https://evilopenai.azure.com",
        "https://openai.azure.com.evil.example", "https://bedrock-runtime.us-east-1.amazonaws.com.evil",
        "https://x.bedrock-runtime.us-east-1.amazonaws.com", "https://bedrock-runtime.xx.amazonaws.com",
        "https://us-east5-aiplatform.googleapis.com.evil", "https://evil.aiplatform.googleapis.com",
        "https://api.anthropic.com@evil.example/v1", "https://evil.example/api.anthropic.com",
    ],
)  # fmt: skip
def test_lookalikes_are_custom(url: str) -> None:
    assert detect(url) == CUSTOM


def test_only_the_hostname_is_read() -> None:
    """Credentials, port, path, query and fragment never reach the result (P1)."""
    url = "https://user:s3cret@API.Anthropic.com.:443/v1/messages?key=sk-abc#frag"
    got = detect(url)
    assert got == ("anthropic", "direct", "api.anthropic.com", None)
    assert "s3cret" not in repr(got)
    assert "sk-abc" not in repr(got)


def test_accepts_non_string_urls_and_bare_hosts() -> None:
    class URL:  # like httpx.URL, which the openai and anthropic clients use for base_url
        def __str__(self) -> str:
            return "https://api.openai.com/v1/"

    assert detect(URL()).provider == "openai"
    assert detect("api.openai.com").provider == "openai"
    assert detect(None) == CUSTOM
    assert detect(12345) == CUSTOM


def test_bad_model_values_do_not_break_bedrock_or_vertex() -> None:
    assert detect("https://bedrock-runtime.us-east-1.amazonaws.com", 42).provider == "custom"
    assert detect("https://aiplatform.googleapis.com", 42).provider == "google"


def test_results_are_valid_wire_values() -> None:
    for url, model, *_ in CASES:
        got = detect(url, model)
        assert got.provider in PROVIDERS
        assert got.route in ROUTES
    assert detect("http://localhost").host is None
    assert detect("https://example.com").region is None
