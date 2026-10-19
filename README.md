# AR Collections Nova Sonic 2.5 Agent

AI-first-initiated **outbound** Accounts Receivable collections voice agent.
Twilio places the outbound call and carries the audio; **Amazon Bedrock Nova
Sonic 2.5** (model `amazon.nova-2-5-sonic`, female voice **`tiffany`**) does the
speech understanding, reasoning, and speech generation; **Strands bi-di agents**
orchestrate the real-time session. The agent greets the debtor by name, states
the exact amount due and due date, converses, and writes the outcome back to the
due record via a tool. Every call is tagged with detailed metrics shown on a
built-in analytics dashboard.

## Architecture

```
  PSTN caller                Twilio Media Streams            This app (FastAPI)
  (phone)  <----- audio ----> (telephony transport) <== WebSocket /media ==>  |
                                                                               |
     mu-law 8kHz  --decode/resample-->  PCM16 16kHz  --> Nova Sonic 2.5 (STT + reasoning + TTS)
     mu-law 8kHz  <--encode/resample--  PCM16 24kHz  <-- voice "tiffany"
```

- **Twilio = telephony transport only.** It dials the callee and relays audio
  over a WebSocket (`wss://<host>/media`) as base64 G.711 mu-law, 8 kHz, mono.
- **Nova Sonic 2.5 = speech-to-speech brain.** It performs STT, reasoning
  (including the `update_due_record` tool call), and TTS in the `tiffany` voice.
- **Audio transcode (`app/audio.py`, stdlib `audioop`):**
  - Inbound: base64 decode -> `ulaw2lin` -> resample **8 kHz -> 16 kHz** -> feed Nova.
  - Outbound: Nova PCM **24 kHz -> 8 kHz** resample -> `lin2ulaw` -> base64 -> Twilio.
  - Resamplers keep `audioop.ratecv` state across chunks so audio stays smooth.
- **Why WSS / ALB idle timeout matters.** Twilio only opens Media Streams over
  **secure WebSocket (wss)**, so the public endpoint must terminate TLS and
  support the WebSocket upgrade. The Application Load Balancer does both; its
  **idle timeout is set to >= 120s** so a long, mostly-listening conversation is
  not dropped mid-call. See Troubleshooting if the socket closes early.
- **Metrics (`app/metrics.py`).** Per call: total end-to-end time, the Twilio
  transport latency vs Nova Sonic active time split, TTFT (time to first audio),
  turn count, and per-turn duration/latency. Emitted as one JSON object per line
  to stdout (CloudWatch-friendly) and persisted for the dashboard.

> **Verified AWS fact:** `amazon.nova-2-5-sonic` is available in Amazon Bedrock
> in **us-east-1** with **streaming = true**. The bi-di operation to grant in
> IAM is `bedrock:InvokeModelWithBidirectionalStream` (plus `bedrock:InvokeModel`).

## Components

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI app: `/healthz`, `/` dashboard, `/api/*` REST, `/media` WebSocket |
| `app/audio.py` | mu-law <-> PCM resample helpers (`audioop`) |
| `app/sonic_bridge.py` | Strands `BidiAgent` + Nova Sonic model wiring and Twilio I/O bridge |
| `app/collections_agent.py` | System prompt + `update_due_record` tool |
| `app/metrics.py` | Per-call / per-turn metrics + JSON-line logging |
| `app/store.py` | Pluggable store: in-memory by default, **DynamoDB on AWS** |
| `app/twilio_client.py` | Places the outbound call with `<Connect><Stream>` TwiML |
| `web/` | Dependency-light vanilla-JS analytics dashboard |
| `scripts/place_call.py` | CLI to initiate an outbound collections call |
| `scripts/deploy.sh` | Operator wrapper for build/lint/push/deploy |
| `infra/template.yaml` | AWS CloudFormation IaC (ECS Fargate + ALB + DynamoDB + CloudWatch + CloudTrail) |

## Requirements

- Python 3.12 (provisioned via [uv](https://docs.astral.sh/uv/)). **Not 3.13** —
  the audio bridge uses the stdlib `audioop` module removed in 3.13.
- AWS account with Bedrock access to `amazon.nova-2-5-sonic` in `us-east-1`.
- A Twilio account (SID/token, a from number, a verified caller id).

## Local end-to-end run

1. **Create the 3.12 virtualenv and install:**

   ```bash
   cd twilio-nova-sonic-2-5
   uv python install 3.12
   uv venv --python 3.12 .venv
   uv pip install -e '.[dev]'
   ```

2. **Set environment from `.env`** (never commit `.env`):

   ```bash
   cp .env.example .env
   # edit .env and fill in the Twilio values
   ```

   | Variable | Default | Notes |
   | --- | --- | --- |
   | `NOVA_MODEL_ID` | `amazon.nova-2-5-sonic` | Bedrock Nova Sonic 2.5 model id |
   | `AWS_REGION` | `us-east-1` | Bedrock region |
   | `TWILIO_ACCOUNT_SID` | _(required)_ | secret, from env only |
   | `TWILIO_AUTH_TOKEN` | _(required)_ | secret, from env only |
   | `TWILIO_FROM_NUMBER` | _(required)_ | your Twilio caller number |
   | `TWILIO_VERIFIED_CALLER_ID` | _(required)_ | callee for a trial/verified setup |
   | `PUBLIC_BASE_URL` | _(required for live calls)_ | public host Twilio reaches |
   | `DUES_TABLE` / `CALLS_TABLE` | _(unset locally)_ | set only on AWS to use DynamoDB |

   When `DUES_TABLE` **and** `CALLS_TABLE` are both set, the app uses DynamoDB;
   otherwise it uses the seeded in-memory store (ideal for local dev/tests).

3. **Start the app:**

   ```bash
   .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
   ```

4. **Expose it publicly so Twilio can reach the WebSocket.** Twilio opens
   `wss://<PUBLIC_BASE_URL>/media`, so the app must be reachable over TLS. For
   local dev use a tunnel, e.g.:

   ```bash
   ngrok http 8080
   # then set PUBLIC_BASE_URL to the https host ngrok prints (no scheme needed),
   # e.g. PUBLIC_BASE_URL=abcd-1-2-3-4.ngrok-free.app, and restart uvicorn.
   ```

5. **Trigger a call** (from the dashboard "Call now" button, the CLI, or REST):

   ```bash
   # CLI
   .venv/bin/python scripts/place_call.py --account-id ACC-1001
   # or dry run (no network, prints the TwiML it would send)
   .venv/bin/python scripts/place_call.py --account-id ACC-1001 --dry-run

   # REST
   curl -X POST localhost:8080/api/calls -H 'content-type: application/json' \
     -d '{"account_id":"ACC-1001"}'
   ```

6. **Watch the dashboard** at `http://localhost:8080/` — it lists due records and
   past calls, and shows per-call metrics (total e2e, Twilio-vs-Nova split, TTFT,
   turn count), a per-turn timeline, and an in-depth event log that updates live
   while a call is active.

## AWS deploy

The IaC (`infra/template.yaml`) provisions an **ECS Fargate** service behind an
**Application Load Balancer** (WebSocket/WSS capable, idle timeout 120s), two
**DynamoDB** tables (dues + call analytics), a **CloudWatch** log group and
**dashboard**, a **CloudTrail** management-event trail to an S3 bucket, and a
**least-privilege IAM task role** granting
`bedrock:InvokeModelWithBidirectionalStream` + `bedrock:InvokeModel` on the Nova
Sonic model ARN, DynamoDB RW on just the two tables, and CloudWatch Logs write.

`scripts/deploy.sh` wraps the steps (run the pieces yourself as needed):

1. **Validate the template (no AWS mutation):**

   ```bash
   scripts/deploy.sh lint       # cfn-lint + aws cloudformation validate-template
   ```

2. **Build and push the image to ECR:**

   ```bash
   scripts/deploy.sh build
   scripts/deploy.sh push       # creates the ECR repo if missing, logs in, pushes
   ```

3. **Provide the Twilio secrets via Secrets Manager or SSM (never inline).**
   Store each value once and pass the **ARNs** to the deploy. For example:

   ```bash
   aws secretsmanager create-secret --name ar/twilio/account_sid --secret-string "$TWILIO_ACCOUNT_SID"
   # ...repeat for auth_token, from_number, verified_caller_id...
   export TWILIO_ACCOUNT_SID_ARN=arn:aws:secretsmanager:us-east-1:920487201358:secret:ar/twilio/account_sid-XXXXX
   export TWILIO_AUTH_TOKEN_ARN=...
   export TWILIO_FROM_NUMBER_ARN=...
   export TWILIO_VERIFIED_CALLER_ID_ARN=...
   ```

   The template references these ARNs in the task definition `Secrets` block; the
   ECS execution role is granted `secretsmanager:GetSecretValue` / `ssm:GetParameter`
   on exactly those ARNs. **No secret value is ever written into the template.**

4. **Deploy:**

   ```bash
   export VPC_ID=vpc-xxxxxxxx
   export PUBLIC_SUBNET_IDS=subnet-aaaa,subnet-bbbb   # >= 2 AZs
   scripts/deploy.sh deploy
   ```

5. **`PUBLIC_BASE_URL` is set automatically to the ALB DNS name.** The template
   injects `PUBLIC_BASE_URL=<ALB DNSName>` into the container environment (via
   `Fn::GetAtt LoadBalancer.DNSName`) and also exports it as the `PublicBaseUrl`
   stack output, so TwiML emits `wss://<alb-dns>/media`. `DUES_TABLE` and
   `CALLS_TABLE` are injected too, which flips the app to the DynamoDB store.

> If you prefer AWS CDK, you can author an equivalent stack; this project ships
> a CloudFormation template because it validates cleanly with `cfn-lint`
> **without** needing `cdk bootstrap`. (Recorded in FEAT-005 findings.)

## Observability

- **CloudWatch Logs.** App logs are one JSON object per line (call placed, stream
  start, per-turn start/stop, TTFT, tool calls, barge-in, errors, call-end
  summary) in the log group `/ecs/<stack>/app`:

  ```bash
  aws logs tail "/ecs/ar-collections-nova-sonic/app" --follow --region us-east-1
  ```

- **CloudWatch Dashboard** (`<stack>-observability`): call volume (ALB request
  count), average TTFT, request latency, and a live log widget. Open the
  `DashboardUrl` stack output in the console.

- **CloudTrail.** A multi-region management-event trail writes to the encrypted,
  private S3 bucket created by the stack. Use it (and CloudWatch) to diagnose
  Bedrock `AccessDenied`, IAM, and deployment issues.

## Troubleshooting

- **Twilio can't reach the WebSocket / 403 on `wss`.** Confirm `PUBLIC_BASE_URL`
  is a publicly reachable TLS host and that the TwiML URL is
  `wss://<PUBLIC_BASE_URL>/media`. Locally, the tunnel (ngrok) must be running and
  `PUBLIC_BASE_URL` must match the current tunnel host. On AWS, the ALB security
  group must allow inbound 80/443 and the service must be healthy on `/healthz`.

- **ALB idle timeout closes the socket mid-call.** The template sets
  `idle_timeout.timeout_seconds = 120`. If you lowered it, a quiet stretch in the
  conversation can trip the idle timeout and drop the WebSocket. Keep it >= 120s.

- **Bedrock `AccessDenied` on `InvokeModelWithBidirectionalStream`.** Check (1)
  **model access** is enabled for `amazon.nova-2-5-sonic`, (2) the **task IAM
  role** grants `bedrock:InvokeModelWithBidirectionalStream` and
  `bedrock:InvokeModel` on `arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-5-sonic`,
  and (3) you are in **region us-east-1** (where the model is verified available
  and streaming). CloudTrail records the denied call for confirmation.

- **Garbled / robotic / chipmunk audio.** Almost always a sample-rate or u-law
  mismatch. Twilio is mu-law 8 kHz mono; Nova input is PCM16 **16 kHz**, output is
  PCM16 **24 kHz**. Verify the resample directions in `app/audio.py` and that the
  per-call `Resampler` state is preserved across chunks.

- **Call drops around 8 minutes.** Nova Sonic has an ~8-minute bi-di connection
  limit. The Strands `BidiAgent` **auto-restarts** the connection (replaying
  history) near the limit — do not disable this. You'll see a
  `BidiConnectionRestartEvent` in the logs; the conversation continues.

- **No metrics on the dashboard.** Metrics are persisted on call end. If a call
  never connected (e.g. Twilio couldn't reach `/media`), there is nothing to show;
  fix the WebSocket reachability first (above).

## Test

```bash
.venv/bin/python -m pytest -q
```
