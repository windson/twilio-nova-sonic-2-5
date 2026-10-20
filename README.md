# AR Collections Nova Sonic 2.5 Agent — an AWS sample

An AWS reference sample showing how to build a **real-time, speech-to-speech
voice agent** on AWS by combining **Twilio** telephony with **Amazon Bedrock
Nova Sonic 2.5** and the **Strands Bidi Agents** GA API, then layering on
**post-call intelligence** and **observability**. The use case is an
AI-initiated **outbound** Accounts Receivable (AR) collections call, but the
architecture is a general blueprint for any phone-based conversational agent.

## What this sample demonstrates

- **Twilio telephony + Amazon Bedrock Nova Sonic 2.5 speech-to-speech.** Twilio
  places the outbound call and carries the audio over a Media Streams
  WebSocket; **Amazon Bedrock Nova Sonic 2.5** (model `amazon.nova-2-5-sonic`,
  voice **`tiffany`**) performs speech understanding, reasoning, and speech
  generation in a single bidirectional stream.
- **Strands GA Bidi Agents.** The real-time session is orchestrated with the
  **generally-available** `strands.bidi` API (`BidiAgent` +
  `BedrockNovaSonicModel`), replacing the earlier experimental
  `strands.experimental.bidi` interface. In-call Strands `@tool`s let the model
  update the due record and capture spoken payment dates mid-conversation.
- **Post-call intelligence.** After the call, an asynchronous pipeline (off the
  real-time path) extracts the **call disposition**, **structured conversation
  data**, and the **next-best-action** via the Bedrock Converse API, resolves
  spoken relative dates with **Amazon Bedrock AgentCore Code Interpreter**, and
  fires downstream automation (payment-link SMS, human escalation).
- **Observability.** Per-call metrics (end-to-end time, TTFT, turn count,
  Twilio-vs-Nova split) feed a built-in analytics dashboard, and the GA Strands
  **OpenTelemetry (OTEL) spans** (per session, per model response, and per tool
  call, including reported token usage) are emitted to CloudWatch Logs.
- **AWS reference architecture.** CloudFront → Application Load Balancer → ECS
  Fargate → Amazon Bedrock, with Amazon DynamoDB persistence, AWS Secrets
  Manager for credentials, CloudWatch for logs/metrics, and CloudTrail for
  audit — all provisioned by a single CloudFormation template.

The analytics dashboard is a **shadcn/ui** React + Tailwind + Vite SPA, compiled
at image-build time and served by the same FastAPI app.

> **Deploying this sample?** Follow [DEPLOYMENT.md](DEPLOYMENT.md) — a
> reproducible, fully-automated (IaC + scripted) runbook that takes an AWS
> account from zero to a live, callable demo with no hidden manual steps.

## AWS services used

| Service | Role in the sample |
| --- | --- |
| **Amazon Bedrock** (Nova Sonic 2.5) | Real-time speech-to-speech model over a bidirectional stream |
| **Amazon Bedrock** (Converse, Nova Lite/Micro) | Post-call disposition + structured-data extraction |
| **Amazon Bedrock AgentCore Code Interpreter** | Off-path resolution of spoken relative dates |
| **Amazon ECS on AWS Fargate** | Runs the FastAPI real-time bridge |
| **Elastic Load Balancing (ALB)** | WebSocket/WSS-capable ingress (idle timeout ≥ 120s) |
| **Amazon CloudFront** | TLS front door for Twilio `wss://` (managed cert) |
| **Amazon DynamoDB** | Dues/account records + per-call analytics |
| **AWS Secrets Manager** | Twilio credentials (referenced by ARN, never inline) |
| **Amazon CloudWatch** | JSON logs, dashboard, and GA OTEL spans |
| **AWS CloudTrail** | Management-event audit trail |
| **AWS CloudFormation** | Single-template IaC for the whole stack |

## Architecture

A full, numbered AWS architecture diagram (Twilio -> CloudFront -> ALB -> ECS ->
Nova Sonic, the in-call tools, DynamoDB, the async post-call intelligence path,
the downstream triggers, and observability) is in
[docs/architecture.md](docs/architecture.md). The quick picture of the live
audio path:

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
  - **Turns are per utterance.** A user utterance is one turn and a Sonic
    utterance is another; the dashboard shows the total count of such turns and
    the duration of each. Consecutive same-role utterances are counted
    separately (not merged). The grouped user<->assistant view is kept
    separately as `conversation_turns` for the chat transcript.
  - **Nova Sonic total time = sum of the durations of Nova's outbound audio
    frames** (PCM16 mono 24 kHz: `ms = bytes / (2 * 24000)`). This replaces the
    earlier response-window sum, which under-counted because Nova reuses a
    single `response_id` across the call.

> **Verified AWS fact:** `amazon.nova-2-5-sonic` is available in Amazon Bedrock
> in **us-east-1** with **streaming = true**. The bidirectional operation to
> grant in IAM is `bedrock:InvokeModelWithBidirectionalStream` (plus
> `bedrock:InvokeModel`).

## Components

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI app: `/healthz`, `/` dashboard, `/api/*` REST, `/media` WebSocket |
| `app/audio.py` | mu-law <-> PCM resample helpers (`audioop`) |
| `app/sonic_bridge.py` | Strands GA `strands.bidi` `BidiAgent` + `BedrockNovaSonicModel` wiring and Twilio I/O bridge |
| `app/telemetry.py` | One-time GA Strands OTEL (`StrandsTelemetry`) setup (console/OTLP exporter) |
| `app/collections_agent.py` | System prompt + `update_due_record` tool |
| `app/metrics.py` | Per-call / per-turn metrics + JSON-line logging |
| `app/store.py` | Pluggable store: in-memory by default, **DynamoDB on AWS** |
| `app/twilio_client.py` | Places the outbound call with `<Connect><Stream>` TwiML |
| `app/code_interpreter.py` | Off-path AgentCore Code Interpreter wrapper (resolves relative date phrases) |
| `app/postcall.py` | Async post-call intelligence (disposition, structured data, NBA, date resolution) |
| `frontend/` | shadcn/ui React + Tailwind + Vite dashboard source (built at image-build time) |
| `web/` | Compiled dashboard bundle served by FastAPI at `/` + `/static/assets/` (git-ignored; produced by the Docker node build stage) |
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
   while a call is active. The turn count is per utterance (each user utterance
   and each Sonic utterance is one turn) and the per-turn table shows the
   duration of each; "Nova Sonic total time" is the summed duration of Nova's
   outbound audio frames.

## Dashboard (shadcn/ui redesign)

The analytics dashboard is a **shadcn/ui** single-page app (React + TypeScript +
Tailwind, bundled with Vite) living in `frontend/`. It replaces the earlier
dependency-light vanilla-JS page while preserving every capability (due records,
past calls, per-call metrics, the chat transcript, the per-turn latency table,
and the in-depth event log) and adds a more interactive information architecture:
a call-attempt header with status/disposition **badges**, the transcript and
telemetry panels, a per-turn latency table, a latency chart, and
**tooltips / collapsibles / popovers** to surface detail on demand. It polls the
same REST API (`/api/config`, `/api/dues`, `/api/calls`, `/api/calls/{id}`) every
few seconds, so a call updates live.

- **Source vs. served bundle.** `frontend/` holds the TypeScript source and
  `shadcn` config; `vite.config.ts` sets `base: "/static/"` and emits the
  compiled bundle into `web/` (`index.html` + `assets/`), which FastAPI serves
  unchanged (`GET /` -> `web/index.html`, `/static` -> `web/`). The compiled
  `web/` bundle is **git-ignored** and produced at image-build time, so no large
  generated assets are committed.
- **Built in the container (multi-stage Docker).** The `Dockerfile` has a
  `node:22-slim` **web-build** stage that runs `npm ci && npm run build` in
  `frontend/` (npm runs at image-build time, where the network is available),
  then the `python:3.12-slim` runtime stage `COPY --from=web-build`s the emitted
  `web/` bundle. Because the node build runs inside `docker build`, the CodeBuild
  environment only needs Docker - no host node toolchain and no local
  `docker build` (the image is built remotely via `scripts/codebuild_image.sh`).

## AWS deploy

> **For a from-scratch, step-by-step deployment, follow
> [DEPLOYMENT.md](DEPLOYMENT.md).** It is a reproducible, fully
> IaC/scripted runbook (prerequisites → Bedrock model access → Twilio secrets
> → CodeBuild image → CloudFormation deploy → seed demo data → force ECS
> rollout → place a call) with no hidden manual steps. The section below is the
> reference explanation of what the IaC provisions and the TLS options.

The IaC (`infra/template.yaml`) provisions an **ECS Fargate** service behind an
**Application Load Balancer** (WebSocket/WSS capable, idle timeout 120s), two
**DynamoDB** tables (dues + call analytics), a **CloudWatch** log group and
**dashboard**, a **CloudTrail** management-event trail to an S3 bucket, and a
**least-privilege IAM task role** granting
`bedrock:InvokeModelWithBidirectionalStream` + `bedrock:InvokeModel` on the Nova
Sonic model ARN, a separate scoped `bedrock:InvokeModel` grant on the text
extraction model ARN for the post-call Converse extraction, a separate
least-privilege `agentcore-code-interpreter` grant
(`bedrock-agentcore:StartCodeInterpreterSession` / `InvokeCodeInterpreter` /
`StopCodeInterpreterSession`, scoped to `code-interpreter/*`) for the off-path
date resolution, DynamoDB RW on just the two tables, and CloudWatch Logs write.
The post-call PTP payment-link SMS needs no new IAM (it reuses existing egress +
the Secrets-Manager Twilio credentials).

> **Code Interpreter / IAM.** The off-path natural-language date tool
> (`app/code_interpreter.py`) targets the **managed** AgentCore interpreter
> `aws.codeinterpreter.v1`, so the task role grants only the three session
> actions its SDK path actually calls
> (`start()` -> `StartCodeInterpreterSession`,
> `invoke("executeCode", ...)` -> `InvokeCodeInterpreter`,
> `stop()` -> `StopCodeInterpreterSession`) and **no** Create/Delete/List/Get.
> Both `bedrock:InvokeModelWithBidirectionalStream` and the `bedrock-agentcore`
> session actions are valid IAM actions missing from cfn-lint's bundled catalog,
> so a scoped `W3037` ignore sits in the TaskRole `Metadata` with a comment.

> **Extraction model / IAM coupling.** The text-extraction model is set once via
> the `ExtractionModelId` CloudFormation parameter (default
> `amazon.nova-lite-v1:0`; also allows `amazon.nova-micro-v1:0`). That single
> parameter drives BOTH the `EXTRACTION_MODEL_ID` container env var AND the
> `Resource` ARN of the `bedrock-text-extraction` IAM grant, so the model the
> app invokes and the model IAM permits can never drift. To use a different
> model, add it to the parameter's `AllowedValues` and redeploy — do not set
> `EXTRACTION_MODEL_ID` out of band, or the Converse call will be access-denied
> at runtime (surfacing as `intelligence_status = failed`).

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
   # Capture the ARN that create-secret returns (shape shown with placeholders):
   export TWILIO_ACCOUNT_SID_ARN=arn:aws:secretsmanager:<region>:<AWS_ACCOUNT_ID>:secret:ar/twilio/account_sid-XXXXXX
   export TWILIO_AUTH_TOKEN_ARN=...
   export TWILIO_FROM_NUMBER_ARN=...
   export TWILIO_VERIFIED_CALLER_ID_ARN=...
   ```

   See [DEPLOYMENT.md](DEPLOYMENT.md) for the exact, copy-pasteable commands.

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
   group's `:80` ingress is restricted to the AWS-managed CloudFront
   origin-facing managed prefix list
   `com.amazonaws.global.cloudfront.origin-facing` (region-specific; resolve the
   id for your region with the `aws ec2 describe-managed-prefix-lists` command
   below and pass it via the `CloudFrontOriginFacingPrefixListId` parameter,
   which defaults to the current us-east-1 id) instead of
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

   #### Origin network isolation: CloudFront VPC origin (active posture)

   The prefix-list allow-list above is an **IP-based** control: it filters by
   CloudFront's published source ranges, which the ALB still exposes to the
   public internet (just filtered at the security group). For stronger,
   **network-level** isolation the reference stack runs the **CloudFront VPC
   origin** posture (`UseVpcOrigin=true`). When both `EnableCloudFront=true` and
   `UseVpcOrigin=true`, the ALB is provisioned as an **internal** load balancer
   (`IpAddressType=ipv4`) in **private subnets with no public IP**, the Fargate
   tasks run private (`AssignPublicIp=DISABLED`, egress via a NAT gateway), and
   CloudFront reaches the origin through a service-managed
   `AWS::CloudFront::VpcOrigin` ENI — so the origin has **no public endpoint at
   all** and is reachable *only* by that distribution. This improves security
   over the prefix-list option because it removes the public IP entirely rather
   than relying on a source-IP allow-list that changes over time. See
   [docs/architecture.md](docs/architecture.md) for the trade-offs.

   The private subnets (internal ALB + tasks) are provisioned by the template
   itself (IaC) when `PRIVATE_SUBNET_IDS` is left blank, so you do **not** need
   to pre-create them; only a public subnet for NAT egress is required. The
   subnets in use are surfaced by the `ProvisionedPrivateSubnetIds` stack output.

   ```bash
   export VPC_ID=<vpc-id>
   export PUBLIC_SUBNET_IDS=<subnet-id-a>,<subnet-id-b>      # still required (2 AZs)
   export ENABLE_CLOUDFRONT=true
   export USE_VPC_ORIGIN=true
   # PRIVATE_SUBNET_IDS left blank — the template provisions the private subnets.
   export NAT_GATEWAY_SUBNET_ID=<public-subnet-for-nat>             # NAT egress for private tasks
   scripts/deploy.sh deploy
   ```

   > **Operational note (this sample's live demo).** VPC origin is the
   > **activated** posture on the reference stack (`UseVpcOrigin=true`, the
   > internal IPv4 ALB). Switching an existing stack from a public ALB to an
   > internal one **replaces** the load balancer (new DNS/ARN — not a
   > zero-downtime change), but the CloudFront **domain is unchanged** across the
   > replacement, so flip `UseVpcOrigin` on a fresh stack or during a planned
   > maintenance window, not as an in-place update of a live stack. One-time
   > account prerequisite: CloudFront auto-provisions the
   > `AWSServiceRoleForCloudFrontVPCOrigins` service-linked role the first time a
   > VPC origin is created.

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
     export CERTIFICATE_ARN=arn:aws:acm:<region>:<AWS_ACCOUNT_ID>:certificate/XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
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

## Post-call intelligence (disposition, structured data, next-best-action)

After a call ends, the app extracts two key artifacts and runs the matching
downstream automation. All of this runs **asynchronously, off the real-time
audio path** (see the no-latency guarantee below), so it cannot add to call
latency. The full flow is drawn as phases 6-9 in
[docs/architecture.md](docs/architecture.md).

### 1. Call Disposition (the "what happened")

A standardized, high-level outcome tag used to update the CRM, route/stop
automated workflows, and stay compliant. The taxonomy is the `CALL_DISPOSITIONS`
tuple in `app/store.py` (disposition is richer than, and separate from, the
`DUE_STATUSES` on the due record):

| Disposition | Meaning | Downstream directive |
| --- | --- | --- |
| `promise_to_pay` (PTP) | Debtor committed to a date/amount | **Send a payment-link SMS and pause** further automated collection (`collection_paused = true`) |
| `dispute_raised` | Debtor disputes the balance | **Escalate to a human specialist** (`escalated = true`) + escalation event |
| `refusal_to_pay` | Debtor explicitly refuses | Route to a human collections strategy queue |
| `inbound_callback_requested` | Debtor will call back / asked for a callback | Schedule a follow-up touchpoint |
| `wrong_number` | Reached the wrong party | Flag so the number is not re-dialed |
| `no_answer` | Nobody picked up | Reschedule per retry policy |
| `left_voicemail` | Voicemail left | Reschedule and wait for callback |
| `already_paid` | Debtor says it is already paid | Verify/reconcile; pause collection |
| `undetermined` | Could not be classified | Leave for human review; no automated trigger |

The two key triggers (`app/postcall.py`):

- **PTP ->** pause auto-collection on the account, then attempt a **real**
  payment-link SMS via `app/twilio_client.send_sms` (reusing the existing Twilio
  REST client + Secrets-Manager credentials; the link is built from
  `PAYMENT_LINK_BASE_URL` + `account_id`).
- **Dispute Raised ->** set `escalated = true` and emit an
  `escalation_to_human_specialist` event.

> **Twilio TRIAL limitation + SMS destination.** The SMS destination is
> **trial-aware** (`TWILIO_TRIAL_MODE`, default `true`):
>
> - **Trial mode (`true`)** — the verified number is the only legal destination,
>   so the payment link is pinned to `TWILIO_VERIFIED_CALLER_ID` regardless of
>   the debtor.
> - **Full account (`false`)** — the SMS addresses the **debtor's own `phone`**
>   from the account record, falling back to the verified id only when the
>   record carries no phone.
>
> The send is always attempted for real; the outcome is **logged and flagged**
> via `ptp_sms_status` (`sent` | `blocked_trial` on a trial block | `failed` on a
> genuine error), **never faked**. The destination is recorded on the call
> record as `ptp_sms_to` in a **masked** form (last four digits only) so an
> auditor can see where the link went without the full number leaking. The demo
> seed records (`ACC-1001`/`ACC-1002`) ship with `phone = null`, so they fall
> back to the verified id until a real number is set.

### 2. Structured Conversation Data & Next-Best-Action (the "why & what's next")

The extracted entities, payment details, and behavioral insights, plus the exact
next step. Stored on the CallAnalytics record (`app/store.py`) as:

```
structured_data:
  payment_specifics:
    promised_amount             # number (stored as Decimal in DynamoDB)
    agreed_date                 # resolved YYYY-MM-DD
    preferred_payment_method    # e.g. card, bank transfer
  contextual_data:
    reason_for_delinquency      # e.g. medical emergency, temporary unemployment
    customer_sentiment          # e.g. cooperative, hostile
next_best_action                # e.g. "Schedule follow-up call on YYYY-MM-DD"
```

These fields are **extracted post-call via the Bedrock Converse API on the TEXT
model `amazon.nova-lite-v1:0`** (`EXTRACTION_MODEL_ID`; `app/postcall.py` prompts
for a strict JSON object and parses it defensively). Until extraction completes
they stay blank (`intelligence_status = pending`); the dashboard renders blank
placeholders and fills them in when `intelligence_status` becomes `ready`.

### 3. Natural-language dates: capture in-call, resolve with a code interpreter

To turn relative phrases the debtor speaks (e.g. "I'll pay next month, the first
Monday") into a concrete date, the work is split so the **real-time turn stays
fast** while the actual computation is delegated to a **real code interpreter**:

- **In-call (fast).** The agent calls a lightweight custom Strands `@tool`,
  `capture_payment_date_phrase(phrase, account_id=None)` in
  `app/collections_agent.py` (same `@tool` pattern as `update_due_record`). It
  only **records the raw phrase** on the due record and returns immediately; it
  does **no** date math and makes **no** network call, so it never adds latency
  to the audio/turn path.
- **Off-path (post-call).** `app/code_interpreter.py` wraps the **AWS Bedrock
  AgentCore Code Interpreter** (`bedrock_agentcore.tools.code_interpreter_client`,
  managed identifier `aws.codeinterpreter.v1`). `resolve_date_phrase(phrase,
  reference_date, region, model_id=None, bedrock_client=None,
  client_factory=None)` first asks a Bedrock **TEXT model** (the same
  `EXTRACTION_MODEL_ID`, via the Converse API) to **author a Python program on
  the fly** - the prompt supplies only the arguments (the phrase + today's
  date), and the **model** reasons from today's date and writes the date math.
  That model-generated program is then shipped to the sandbox via
  `invoke("executeCode", {"language": "python", "code": ...})`, and the wrapper
  parses the `YYYY-MM-DD` the program prints. The orchestration never
  hand-writes the date logic; a tiny labelled harness only guarantees a single
  printed line. Both the model client (`bedrock_client`) and the sandbox client
  (`client_factory`) are injectable so tests never hit AWS, and the wrapper is
  fully defensive (any error / `isError` / unparseable output returns `None`,
  and the session is always `stop()`-ped in a `finally`).

**Why off-path:** a Code Interpreter session costs roughly **~1.9s** (~1.75s to
start + ~0.13s to execute), which is why the heavy resolution runs only inside
the post-call task (`extract_and_apply` in `app/postcall.py`) and never on the
real-time turn. The already-ISO fast path (`strptime`) skips the interpreter
entirely when the model already returned a concrete date.

Example: with a reference date in May 2024, `"next month 1st Monday"` resolves to
`2024-06-03`. Other handled phrases include `"in two weeks"`, `"next Friday"`,
`"end of next month"`, and `"tomorrow"`.

### Async / no-latency guarantee

The in-call tools (`update_due_record`, `capture_payment_date_phrase`) are cheap
and local, but the **expensive work** (LLM disposition classification +
structured extraction + the AgentCore code-interpreter date resolution + the
downstream triggers) runs **after the call ends**, off the
audio/turn path, as a fire-and-forget `asyncio` task launched from the `/media`
WebSocket finally-block in `app/main.py` (`extract_and_apply` in
`app/postcall.py`). The call-latency metrics (`total_e2e_ms`, `nova_total_ms`,
`ttft_ms`) are computed on the audio path (`session_start`..`session_end`) and
are frozen before the post-call task runs, so it provably cannot inflate them.

This is enforced by two tests in `tests/test_postcall.py`:
`test_no_added_latency_postcall_does_not_touch_call_metrics` drives a synthetic
`CallMetrics` session to fixed values, then runs `extract_and_apply` and asserts
the three metrics are **byte-for-byte unchanged**; and
`test_no_latency_audio_path_never_invokes_extraction` drives the full audio path
with a blocking Bedrock stub **and a blocking code-interpreter stub**, asserting
neither is touched until the off-path task runs post-call.

## Observability

- **GA Strands OTEL spans.** Because the session is orchestrated with the GA
  `strands.bidi` API, the agent loop automatically emits OpenTelemetry spans
  once `StrandsTelemetry` is configured **before** the agent runs — one span per
  **session** (with `gen_ai.usage.*` token totals), per **model response** (with
  native time-to-first-audio), and per **tool call**, including reported token
  usage. The app wires this once at startup (`app/telemetry.py`, guarded so a
  setup failure never crashes the app or touches the real-time audio path) and
  defaults to the **console exporter**, so spans are written to stdout and the
  ECS `awslogs` driver ships them to CloudWatch Logs with **no new IAM and no
  new infrastructure**. Set `OTEL_EXPORTER=otlp` (+ `OTEL_EXPORTER_OTLP_ENDPOINT`)
  to send to a collector, or `OTEL_ENABLED=false` to disable. These non-secret
  knobs are surfaced via `GET /api/config`. See
  [docs/architecture.md](docs/architecture.md) for how OTEL reconciles with the
  dashboard's `CallMetrics`.

- **CloudWatch Logs.** App logs are one JSON object per line (call placed, stream
  start, per-turn start/stop, TTFT, tool calls, barge-in, errors, call-end
  summary) in the log group `/ecs/<stack>/app`:

  ```bash
  aws logs tail "/ecs/ar-collections-nova-sonic/app" --follow --region us-east-1
  ```

- **CloudWatch Dashboard** (`<stack>-observability`): call volume (ALB request
  count), average TTFT, request latency, and a live log widget. Open the
  `DashboardUrl` stack output in the console. The dashboard's
  `ARCollections/TTFTms` widget is an aspirational custom-metric placeholder:
  nothing emits that metric yet, so it reads empty. Wiring it live would mean
  emitting per-call custom metrics via `cloudwatch:PutMetricData` (POC approach
  B, which would also add that IAM action to the task role).

- **Per-call CloudWatch deep link (POC approach A).** Each row in the dashboard
  "Past calls" table has a **Logs** link that opens the app log group's
  CloudWatch Logs "Log events" view pre-filtered to that call's `call_sid`. It
  is built entirely client-side from `aws_region` + `app_log_group` (both
  surfaced by `GET /api/config`, with `APP_LOG_GROUP` set on the task), so it
  needs no extra IAM and no metrics pipeline. The `call_sid` is passed as a
  **plain `filterPattern` term** (not quoted): the Logs "Log events" filter
  treats a bare alphanumeric token as a term match, which is what a Twilio
  `call_sid` is, so a plain term matches reliably where a quoted phrase can not.
  The link uses `stopPropagation` so clicking it does not also select the row.

- **Conversational turns (what `turn_count` means).** A turn is one side of the
  user<->Nova Sonic alternation: a caller utterance answered by a Sonic utterance
  is the back-and-forth. `turn_count` is derived from the GA
  `BidiTranscriptBlockEvent` `role` (`user`/`assistant`) alternation — each block
  event is a completed utterance — not from Nova response IDs. This **replaced
  the earlier `response_id`-based
  grouping** because in live calls Nova Sonic reused a single `response_id` for
  the entire exchange, so counting matched response-start/response-complete pairs
  reported `turn_count = 1` even when there were several user<->Sonic
  alternations. The `response_id` grouping is retained only as a secondary
  Nova-response timing signal (`nova_response_count`). See
  `CallMetrics._build_conversation_turns` in `app/metrics.py`.

- **Conversation (chat) view.** The call detail panel reconstructs the call as a
  chat transcript (caller vs Nova Sonic, ordered by start time, in text),
  showing the time each turn took and, for Sonic turns, TTFT and latency. It is
  driven by the `conversation_turns` field from `GET /api/calls/{id}` and falls
  back to the raw `transcripts` for older records. The raw per-event JSON log
  remains available under the collapsible "In-depth event log" panel.

  - **Per-turn total time** (`total_ms`) is the elapsed time the turn occupied
    the conversation — the span from the turn's start to the **next** turn's
    start (the last turn falls back to its own transcript span, then the session
    end). This is deliberately *not* `end − start` within the turn, because most
    turns are a single final transcript whose intra-segment span is 0 and would
    read as a broken "0 ms" measurement. The raw intra-segment span is still
    available as `transcript_span_ms`.
  - **Sonic TTFT / latency.** Because Nova Sonic can reuse one `response_id` for
    the whole call, the response-window TTFT is only known for the turn that
    maps to that window. Every Sonic turn still gets a real **latency**
    (`latency_ms`), derived from the preceding caller turn's last final
    transcript to this Sonic turn's first final transcript. **TTFT** is shown
    when it comes from a response window (labelled `TTFT (response)`, with
    `ttft_source = "response_window"`); for later turns under a shared
    `response_id` Nova emits no per-utterance audio marker, so TTFT is honestly
    labelled unavailable (`ttft_source = "unavailable"`) rather than left
    silently blank.

- **Model invocation logging is intentionally NOT enabled.** Bedrock model
  invocation logging only captures `Converse`/`ConverseStream`/`InvokeModel`/
  `InvokeModelWithResponseStream`. Nova Sonic bi-directional streaming uses
  `InvokeModelWithBidirectionalStream`, which that feature does not cover, so it
  would not record any turn/TTFT ground truth here (and there is no
  CloudFormation resource for it — it is an account-level API). Turn semantics
  are therefore derived from the GA Strands `BidiTranscriptBlockEvent`
  user/assistant alternation plus response-start/first-audio timing. See the
  comment near the turn-counting logic in `app/metrics.py`. The GA
  `strands.bidi` agent loop also emits authoritative OTEL spans (session token
  usage, per-response native TTFT, tool calls) — see the Observability notes
  below and `docs/architecture.md`.

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
