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
- **TLS front door (Option 1: CloudFront).** The simplest way to give Twilio a
  trusted `wss://` endpoint without owning a custom domain is to put an **Amazon
  CloudFront** distribution in front of the HTTP-only ALB. CloudFront serves the
  free, AWS-managed `*.cloudfront.net` certificate, so the public endpoint is
  `wss://<dist>.cloudfront.net/media` and TwiML emits that URL. CloudFront
  forwards the WebSocket upgrade to the ALB over HTTP. This is the deployed
  configuration; see **AWS deploy -> Option 1** and **Troubleshooting**.
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

5. **Enable TLS for live calls (required by Twilio).** Twilio Media Streams
   only connect over `wss://` (TLS); a plaintext `ws://` endpoint is refused.
   The raw ALB DNS name has **no certificate**, so you must front it with TLS.
   There are two supported options.

   ### Option 1 (recommended, no custom domain): CloudFront in front of the ALB

   Put an Amazon CloudFront distribution in front of the HTTP-only ALB and use
   the **free AWS-managed `*.cloudfront.net` certificate** — no ACM certificate,
   no DNS record, and no domain you must own. The template gates this behind the
   `EnableCloudFront` parameter:

   ```bash
   export VPC_ID=vpc-xxxxxxxx
   export PUBLIC_SUBNET_IDS=subnet-aaaa,subnet-bbbb   # >= 2 AZs
   export ENABLE_CLOUDFRONT=true
   scripts/deploy.sh deploy     # adds EnableCloudFront=true to the stack
   ```

   With `EnableCloudFront=true` the stack creates an `AWS::CloudFront::Distribution`
   whose single origin is the ALB DNS over **`http-only`** (the ALB has no cert),
   and sets `PUBLIC_BASE_URL` to the distribution domain so TwiML emits
   `wss://<dist>.cloudfront.net/media`. The distribution domain is exposed as the
   `CloudFrontDomainName` stack output. CloudFront deploys in ~5-15 min; the stack
   finishes `UPDATE_COMPLETE` once it is live. For the WebSocket upgrade to pass
   through, the default cache behavior uses:

   - the managed **AllViewer** origin-request policy
     (`216adef6-5c7f-47e4-b989-5492eafa07d3`) so the `Upgrade`, `Connection`, and
     all `Sec-WebSocket-*` headers are forwarded to the ALB;
   - the managed **CachingDisabled** cache policy
     (`4135ea2d-6df8-44a3-9df3-4b5a84be39ad`);
   - `ViewerProtocolPolicy: redirect-to-https`, all 7 methods allowed;
   - `OriginReadTimeout` and `OriginKeepaliveTimeout` set to **60s** (the max).

   **ALB ingress is locked to CloudFront when `EnableCloudFront=true`.** To make
   the CloudFront TLS front door the *only* ingress path (so a plaintext
   `ws://<alb-dns>/media` can no longer bypass CloudFront), the ALB security
   group's `:80` ingress is restricted to the AWS-managed CloudFront prefix list
   `com.amazonaws.global.cloudfront.origin-facing` (us-east-1 id `pl-3b927c52`,
   passed via the `CloudFrontOriginFacingPrefixListId` parameter) instead of
   `0.0.0.0/0`. CloudFront talks to the origin over `http-only:80`, so only the
   `:80` rule is needed; adding a second `:443` prefix-list rule is unnecessary
   and would exceed the 60-rule-per-SG limit (the managed prefix list has ~49
   entries, each of which counts toward that quota). When `EnableCloudFront=false`
   the original open `0.0.0.0/0` ingress on `:80`/`:443` is used instead. Resolve
   the prefix list id for a region with:

   ```bash
   aws ec2 describe-managed-prefix-lists \
     --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
     --region us-east-1 --query 'PrefixLists[0].PrefixListId' --output text
   ```

   Verify the boundary after deploy: `https://<dist>.cloudfront.net/healthz`
   still returns **200**, while a direct `curl --max-time 10 http://<alb-dns>/healthz`
   now **hangs/times out** (connection no longer accepted from outside CloudFront).

   Verify after deploy:

   ```bash
   CF=$(aws cloudformation describe-stacks --stack-name ar-collections-nova-sonic \
     --region us-east-1 \
     --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue|[0]" \
     --output text)
   curl -sS -o /dev/null -w '%{http_code}\n' https://$CF/healthz   # -> 200 (trusted cert, no -k)
   ```

   ### Option 2 (custom domain): ACM certificate + HTTPS listener on the ALB

   If you own a hostname, terminate TLS directly on the ALB with an ACM
   certificate on that real hostname:

   - Request/import an ACM certificate (in the stack's region, `us-east-1`) for
     a hostname you control, e.g. `collections.example.com`.
   - Create a DNS record (CNAME, or an alias/A-ALIAS) pointing that hostname at
     the ALB. The raw ALB DNS is exported as the `AlbDnsName` stack output.
   - Pass both parameters so the template adds an **HTTPS:443 listener** and
     points `PUBLIC_BASE_URL` at the TLS hostname:

     ```bash
     export CERTIFICATE_ARN=arn:aws:acm:us-east-1:920487201358:certificate/XXXX
     export PUBLIC_HOSTNAME=collections.example.com
     scripts/deploy.sh deploy   # pass CertificateArn / PublicHostname params
     ```

   With `CertificateArn` set the stack creates an HTTPS:443 listener
   (`ELBSecurityPolicy-TLS13-1-2-2021-06`) forwarding to the app, and with
   `PublicHostname` set `PUBLIC_BASE_URL` becomes that hostname so TwiML emits
   `wss://<hostname>/media`. The `HttpsEnabled` stack output reports whether TLS
   was provisioned.

   > **Do not use the raw ALB DNS as `PUBLIC_BASE_URL` for a live call.** It has
   > no TLS certificate, so `wss://<alb-dns>/media` will fail the Twilio
   > handshake. Omitting `CertificateArn`/`PublicHostname` yields an HTTP:80-only
   > ALB that is fine for `/healthz` and internal testing but cannot accept the
   > Twilio WebSocket.

   `DUES_TABLE` and `CALLS_TABLE` are injected automatically either way, which
   flips the app to the DynamoDB store.

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

- **CloudFront (Option 1): `wss` upgrade fails with 403/502 or the handshake
  hangs.** The WebSocket upgrade only passes through CloudFront when the cache
  behavior forwards the upgrade headers. Confirm the default behavior uses the
  managed **AllViewer** origin-request policy (forwards `Upgrade`/`Connection`/
  `Sec-WebSocket-*`) and the managed **CachingDisabled** cache policy, that the
  origin protocol is **`http-only`** (the ALB listens on HTTP:80 with no cert),
  and that the ALB target group is healthy on `/healthz`. Validate the upgrade
  end-to-end before dialing by opening a client socket to
  `wss://<dist>.cloudfront.net/media`; a **101 Switching Protocols** handshake and
  an open socket (the app calls `websocket.accept()` immediately) is the pass
  signal, corroborated by a `stream`/metrics `stream_start` log line in
  `/ecs/<stack>/app`.

- **CloudFront origin timeout vs. long silences (validated finding).**
  CloudFront's `OriginReadTimeout`/`OriginKeepaliveTimeout` (default 30s, raised
  to the **60s** max here) applies to the **initial HTTP response** — i.e. the
  `101 Switching Protocols` handshake — **not** to an already-established,
  long-lived WebSocket. Once the socket is upgraded, CloudFront holds the
  persistent TCP connection open, so silent stretches on an in-progress Media
  Streams call do **not** trip the 60s origin-response ceiling. This was observed
  empirically on a live call (silent gaps of several seconds while the agent
  spoke and the debtor listened did not cause any CloudFront/ALB-initiated
  disconnect). The governing idle timeout for an established socket is the ALB's
  (120s), and Nova Sonic auto-reconnects near its ~8-minute bi-di limit. CloudFront
  is therefore a safe TLS front door for this workload; the only residual risk is
  a near-silent conversation that exceeds the ALB idle timeout, which the app
  mitigates with Twilio `mark` keepalives and the Nova auto-restart.

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
