# Twilio and Amazon Nova Sonic 2.5

A phone-based conversational application connecting Twilio Media Streams to
Amazon Nova Sonic 2.5 through the Strands bidirectional agent SDK. The example
use case is an outbound accounts-receivable conversation: confirm the intended
person, discuss an outstanding balance, and record an agreed next step.

The Python service handles audio, account records and post-call processing.
A React dashboard displays calls, transcripts, response timing, model usage
and conversation intelligence.

## Features

- **Streaming conversation:** one Nova Sonic session receives speech and
  produces speech, text and tool calls. Stateful audio conversion connects
  Twilio's 8 kHz μ-law stream to Nova's PCM formats.
- **English, French and Spanish:** the Tiffany voice follows the caller's
  supported language within the same session. The opening greeting asks for
  identity confirmation before discussing account details.
- **Native asynchronous tools:** account updates and payment-date capture
  run alongside audio. A call controller coordinates tool completion,
  Nova-generated farewells and Twilio playback acknowledgement.
- **Post-call intelligence:** after confirmed telephone completion, a separate
  worker calculates Sonic cost, then uses OpenAI GPT-6 Luna through Amazon
  Bedrock to extract outcomes and next actions. Payment dates use a Python
  parser with an AgentCore Code Interpreter fallback.
- **Usage and cost:** accumulated text/audio input/output token counts are
  priced from the configured region's AWS list-price snapshot. Missing usage
  remains explicitly incomplete.
- **Dashboard:** nested account/call tables, transcripts, tool activity,
  response timelines, token breakdowns and light/dark themes.
- **Infrastructure as code:** CloudFormation defines ECS Fargate, load
  balancing, optional CloudFront ingress, DynamoDB, observability and the
  CodeBuild container build.

## Architecture

![Runtime architecture](docs/diagrams/architecture.png)

The [architecture guide](docs/architecture.md) describes the current code,
execution boundaries, storage and infrastructure options. It includes
[runtime](docs/diagrams/architecture.svg),
[call-processing](docs/diagrams/call-processing.svg), and
[build](docs/diagrams/deployment.svg) diagrams.
[Diagram sources and icon terms](docs/diagrams/README.md) are included.

## Local development

Use **Python 3.12** and **Node.js 22**. The audio adapter uses `audioop`, which
was removed from the Python standard library in 3.13.

From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm --prefix frontend ci
npm --prefix frontend run build
cp .env.example .env
.venv/bin/python -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8080
```

Open [localhost:8080](http://localhost:8080/). The frontend build writes to
the ignored `web/` directory, which FastAPI serves alongside the API.
Rebuild the frontend after editing its source.

The default store contains example records in process memory. Live calls
require your own Twilio configuration, AWS credentials with access to the
configured Bedrock models, and a public HTTPS endpoint that supports the
`/media` WebSocket. Twilio must be able to reach that endpoint over WSS.

## Configuration

Copy the variable names from [.env.example](.env.example) and keep credentials
outside version control. The service reads its process environment; the local
command above loads `.env` through Uvicorn.

| Variable | Purpose |
| --- | --- |
| `AWS_REGION` | AWS region used by the model and service clients; defaults to `us-east-1`. |
| `NOVA_MODEL_ID` | Speech model; defaults to `amazon.nova-2-5-sonic`. |
| `EXTRACTION_MODEL_ID` | Post-call text model; defaults to `us.openai.gpt-6-luna`. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Credentials for your Twilio account. |
| `TWILIO_FROM_NUMBER` | Twilio number used for outbound calls. |
| `TWILIO_VERIFIED_CALLER_ID` | Destination used by the application's call-creation route. |
| `PUBLIC_BASE_URL` | Public host Twilio connects to for the media stream. |
| `DUES_TABLE`, `CALLS_TABLE` | DynamoDB table names; both must be set to enable DynamoDB persistence. |
| `PAYMENT_TIMEZONE` | Timezone for interpreting payment dates; defaults to `UTC`. |
| `PAYMENT_LINK_BASE_URL` | Base URL for payment links; the application does not process payments. |
| `OTEL_ENABLED`, `OTEL_EXPORTER` | Enable OpenTelemetry and select `console`, `otlp` or `none`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint when the OTLP exporter is selected. |
| `NOVA_ENDPOINTING_SENSITIVITY` | Optional `HIGH`, `MEDIUM` or `LOW`; empty preserves the provider default. |

[Runtime settings](app/config.py) define the full configuration.
[CloudFormation parameters](infra/template.yaml) define infrastructure choices.
Model availability and access must match the selected region.

## Source layout

| Path | Responsibility |
| --- | --- |
| `app/main.py` | HTTP API, WebSocket entry point, persistence and workflow launch. |
| `app/sonic_bridge.py`, `app/audio.py` | Strands session and Twilio audio adapters. |
| `app/collections_agent.py`, `app/call_lifecycle.py` | Conversation prompt, tools and telephone lifecycle. |
| `app/metrics.py`, `app/sonic_usage.py` | Call timing, transcripts and token accounting. |
| `app/postcall_workflow.py`, `app/postcall.py` | Post-disconnect costing, intelligence and eligible actions. |
| `app/sonic_pricing.py` | Deterministic regional Sonic cost calculation. |
| `app/date_resolver.py`, `app/code_interpreter.py` | Payment-date interpretation. |
| `app/store.py`, `app/twilio_client.py` | Storage and telephony clients. |
| `frontend/` | React, TypeScript, Tailwind and Vite dashboard. |
| `tests/`, `frontend/tests/` | Backend and frontend tests. |
| `infra/`, `Dockerfile` | CloudFormation, source verification and container build. |

## Tests

```sh
.venv/bin/python -m pytest
npm --prefix frontend test
npm --prefix frontend run build
```

The tests cover lifecycle ordering, audio and asynchronous tools, usage
accounting, date resolution, post-call admission, API behavior and dashboard
data presentation.

## References

- [Amazon Nova 2.5 Sonic responsible AI overview](https://docs.aws.amazon.com/ai/responsible-ai/nova-2-5-sonic/overview.html)
- [Amazon Nova speech-to-speech code examples](https://docs.aws.amazon.com/nova/latest/userguide/s2s-example.html)
- [Amazon Nova 2.5 Sonic announcement](https://aws.amazon.com/about-aws/whats-new/2026/10/amazon-nova-2.5-Sonic/)
- [Amazon Nova pricing](https://aws.amazon.com/nova/pricing/)
- [Amazon CloudFront VPC origin dual-stack announcement](https://aws.amazon.com/about-aws/whats-new/2026/10/cloudfront-vpc-origin-dualstack/)

## AWS disclaimer

This repository is a community example, not an official AWS product or support
offering. The code and documentation are provided "as is", without warranties
or guarantees. References to AWS services, documentation and architecture icons
do not imply AWS endorsement.

Before deployment, validate the code against your security and operational
requirements and review current regional service availability, pricing and
applicable AWS terms. AWS names and logos are trademarks of Amazon.com, Inc.
or its affiliates. Other product names and logos belong to their respective
owners.
