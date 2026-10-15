"""Azure OpenAI, Amazon Bedrock and Google Vertex AI, measured by Gaslit.

Run from sdk/python:  uv run python ../../examples/cloud_hosts.py

Gaslit tells routes apart by hostname only (brief §6.6). In the payload, look at ``route``,
``endpoint_host``, ``endpoint_region`` and the model fields:

- Azure: the customer's resource name is stripped from the host, and the deployment name is
  reported as ``azure-deployment``, since both are the customer's own names.
- Bedrock and Vertex: the region comes from the host, the model vendor from the model id.

The clients use fake credentials, and the requests go to the local fake provider.
"""

import anthropic
import gaslit
import openai
from _demo import Behavior, offline

gaslit.init(debug=True, service="cloud-demo", sample_rate=1.0)

messages = [{"role": "user", "content": "Say hello."}]

with offline() as fake:
    azure = openai.AzureOpenAI(
        azure_endpoint="http://acme-corp.openai.azure.com",
        api_version="2024-10-21",
        api_key="fake",
        max_retries=0,
    )
    fake.script(Behavior(model="gpt-4o-2024-08-06"))  # Azure reports the model it ran
    reply = azure.chat.completions.create(model="acme-gpt4o-prod", messages=messages)
    print("azure:", reply.choices[0].message.content)

    bedrock = anthropic.AnthropicBedrock(
        api_key="fake",  # a Bedrock API key; AWS credentials work the same way
        aws_region="eu-central-1",
        base_url="http://bedrock-runtime.eu-central-1.amazonaws.com",
        max_retries=0,
    )
    message = bedrock.messages.create(
        model="eu.anthropic.claude-sonnet-4-5-20250929-v1:0", max_tokens=100, messages=messages
    )
    print("bedrock:", message.content[0].text)

    vertex = anthropic.AnthropicVertex(
        region="us-east5",
        project_id="acme-project",
        access_token="fake",  # noqa: S106 (a placeholder)
        base_url="http://us-east5-aiplatform.googleapis.com/v1",
        max_retries=0,
    )
    with vertex.messages.stream(
        model="claude-sonnet-4-5@20250929", max_tokens=100, messages=messages
    ) as stream:
        print("vertex:", "".join(stream.text_stream))

gaslit.flush()
