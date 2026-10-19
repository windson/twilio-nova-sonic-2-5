# AR Collections Nova Sonic 2.5 Agent

AI-first-initiated outbound Accounts Receivable Collections voice agent. Twilio
places the outbound call and bridges Media Streams audio over a WebSocket to
Amazon Bedrock Nova Sonic 2.5 speech-to-speech (model `amazon.nova-2-5-sonic`,
female voice `tiffany`), orchestrated via Strands bi-di agents. The app captures
per-call and per-turn metrics and surfaces them on a web analytics UI.

> Status: FEAT-001 establishes the project scaffold, Python 3.12 environment,
> env template, and a runnable FastAPI skeleton with a `/healthz` endpoint.
> The Twilio bridge, Nova Sonic wiring, metrics, UI, and AWS IaC are added in
> later features.

## Requirements

- Python 3.12 (provisioned via [uv](https://docs.astral.sh/uv/))
- AWS account with Bedrock access to `amazon.nova-2-5-sonic` in `us-east-1`
- A Twilio account (SID/token, a from number, a verified caller id)

## Setup

```bash
cd twilio-nova-sonic-2-5
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
```

Copy the environment template and fill in your values (never commit `.env`):

```bash
cp .env.example .env
```

Environment variables (read at runtime from `os.environ`):

| Variable | Default | Notes |
| --- | --- | --- |
| `NOVA_MODEL_ID` | `amazon.nova-2-5-sonic` | Bedrock Nova Sonic 2.5 model id |
| `AWS_REGION` | `us-east-1` | Bedrock region |
| `TWILIO_ACCOUNT_SID` | _(required)_ | secret, from env only |
| `TWILIO_AUTH_TOKEN` | _(required)_ | secret, from env only |
| `TWILIO_FROM_NUMBER` | _(required)_ | caller number |
| `TWILIO_VERIFIED_CALLER_ID` | _(required)_ | callee for verified/trial setups |
| `PUBLIC_BASE_URL` | _(required for live calls)_ | public URL Twilio reaches |

## Run

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Then:

- `GET /healthz` returns `{"status": "ok"}`
- `GET /` serves the landing page (analytics UI added later)

## Test

```bash
.venv/bin/python -m pytest -q
```
