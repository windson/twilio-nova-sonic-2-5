# Deployment guide

A reproducible, end-to-end runbook to deploy this AWS sample from scratch into
**your own AWS account**. The infrastructure is provisioned by a single
CloudFormation template (`infra/template.yaml`); the image build and rollout are
scripted. Every step that earlier iterations did out-of-band (Bedrock model
access, Twilio secrets, demo-data seeding, the forced ECS rollout) is called out
explicitly below so there are **no hidden manual surprises**.

All commands target **`us-east-1`**, where Nova Sonic 2.5, Amazon Bedrock
AgentCore Code Interpreter, and CloudFront VPC origins are all available. Set
`AWS_REGION=us-east-1` or pass `--region us-east-1` throughout.

> **Secrets.** Twilio credentials are stored in AWS Secrets Manager and
> referenced by **ARN**. No secret value is ever placed in the template, the
> scripts, or an environment-variable override passed to CloudFormation.

---

## 0. Overview — the shape of a deploy

```
prerequisites  ->  store Twilio secrets  ->  build image (CodeBuild -> ECR)
      ->  cloudformation deploy (IaC)  ->  seed demo dues  ->  force ECS rollout
      ->  open the dashboard + place a call
```

| # | Step | Automated by |
| - | --- | --- |
| 1 | Prerequisites + Bedrock model access | manual (one-time console/CLI) |
| 2 | Store Twilio secrets in Secrets Manager | `aws secretsmanager create-secret` |
| 3 | Collect VPC / subnet inputs | manual (your existing VPC) |
| 4 | Build the container image | `scripts/codebuild_image.sh` (CodeBuild → ECR) |
| 5 | Deploy the stack (IaC) | `scripts/deploy.sh deploy` |
| 6 | Seed the demo dues data | `scripts/seed_dues.py` |
| 7 | Force a new ECS deployment | `scripts/deploy.sh redeploy` |
| 8 | Reach the dashboard + place a call | dashboard / `scripts/place_call.py` |

---

## 1. Prerequisites

1. **An AWS account** with permissions to create ECR, CodeBuild, S3, IAM, ECS,
   ELBv2, DynamoDB, CloudWatch, CloudFront, CloudTrail, and Secrets Manager
   resources, and the **AWS CLI v2** configured for `us-east-1`:

   ```bash
   aws configure            # or use an SSO/role profile
   aws sts get-caller-identity --region us-east-1   # confirm identity
   ```

2. **Python 3.12** (for the scripts and local tooling). **Not 3.13** — the audio
   bridge uses the stdlib `audioop` module removed in 3.13. Create the venv:

   ```bash
   cd twilio-nova-sonic-2-5
   uv python install 3.12 && uv venv --python 3.12 .venv
   uv pip install -e '.[dev]'
   ```

3. **Enable Amazon Bedrock model access** (one-time, per account/region). In the
   Bedrock console → **Model access**, request/enable access for:

   - **`amazon.nova-2-5-sonic`** — the real-time speech-to-speech model.
   - **the text extraction model** used by post-call intelligence —
     `amazon.nova-lite-v1:0` (default) or `amazon.nova-micro-v1:0` (the
     `ExtractionModelId` CloudFormation parameter picks which).

   Without model access, calls fail with Bedrock `AccessDenied`.

4. **A Twilio account** with:

   - a **phone number** (the "from" number),
   - your **Account SID** and **Auth Token**, and
   - for a **trial** account, a **verified caller id** (the only number a trial
     account may call/SMS). The demo dials `TWILIO_VERIFIED_CALLER_ID`.

5. **Region support.** Use **`us-east-1`**: Nova Sonic 2.5, AgentCore Code
   Interpreter, and CloudFront VPC origins are all available there.

---

## 2. Store the Twilio secrets in Secrets Manager

Store each value **once** and keep only the returned ARN. Never inline a secret
value into the template, a script, or shell history you commit.

```bash
export AWS_REGION=us-east-1

aws secretsmanager create-secret --region "$AWS_REGION" \
  --name ar/twilio/account_sid       --secret-string "$TWILIO_ACCOUNT_SID"
aws secretsmanager create-secret --region "$AWS_REGION" \
  --name ar/twilio/auth_token        --secret-string "$TWILIO_AUTH_TOKEN"
aws secretsmanager create-secret --region "$AWS_REGION" \
  --name ar/twilio/from_number       --secret-string "$TWILIO_FROM_NUMBER"
aws secretsmanager create-secret --region "$AWS_REGION" \
  --name ar/twilio/verified_caller_id --secret-string "$TWILIO_VERIFIED_CALLER_ID"
```

Each `create-secret` returns an `ARN`. Capture the four ARNs (shape shown with
placeholders — `<region>` / `<AWS_ACCOUNT_ID>` are filled by your account):

```bash
export TWILIO_ACCOUNT_SID_ARN=$(aws secretsmanager describe-secret \
  --region "$AWS_REGION" --secret-id ar/twilio/account_sid --query ARN --output text)
export TWILIO_AUTH_TOKEN_ARN=$(aws secretsmanager describe-secret \
  --region "$AWS_REGION" --secret-id ar/twilio/auth_token --query ARN --output text)
export TWILIO_FROM_NUMBER_ARN=$(aws secretsmanager describe-secret \
  --region "$AWS_REGION" --secret-id ar/twilio/from_number --query ARN --output text)
export TWILIO_VERIFIED_CALLER_ID_ARN=$(aws secretsmanager describe-secret \
  --region "$AWS_REGION" --secret-id ar/twilio/verified_caller_id --query ARN --output text)
# ARNs look like: arn:aws:secretsmanager:<region>:<AWS_ACCOUNT_ID>:secret:ar/twilio/account_sid-XXXXXX
```

The template wires these ARNs into the task definition `Secrets` block and grants
the ECS execution role `secretsmanager:GetSecretValue` on exactly those ARNs.

---

## 3. VPC / subnet inputs

The stack deploys into an **existing** VPC. Collect:

- `VPC_ID` — the target VPC id.
- `PUBLIC_SUBNET_IDS` — two public subnet ids in **≥ 2 AZs** (comma-separated),
  for the internet-facing ALB and the Fargate tasks (default posture).

```bash
export VPC_ID=<vpc-id>
export PUBLIC_SUBNET_IDS=<subnet-id-a>,<subnet-id-b>
```

**Optional — CloudFront VPC origin (stronger origin isolation).** To run the
ALB internal (no public IP, reachable only via CloudFront's VPC origin ENI),
also provide private subnets and a NAT subnet and set `USE_VPC_ORIGIN=true`:

```bash
export USE_VPC_ORIGIN=true
export PRIVATE_SUBNET_IDS=<private-subnet-a>,<private-subnet-b>  # internal ALB + tasks
export NAT_GATEWAY_SUBNET_ID=<public-subnet-for-nat>            # NAT egress for private tasks
```

Notes for the VPC-origin option:
- The first VPC origin in an account triggers a **one-time** auto-provisioning of
  the `AWSServiceRoleForCloudFrontVPCOrigins` service-linked role (no action
  needed; just be aware it appears).
- Switching an **existing** stack from a public ALB to an internal one
  **replaces** the load balancer (new DNS/ARN — not zero-downtime). Prefer a
  fresh stack or a planned window. This sample's reference deploy keeps
  `UseVpcOrigin=false`. See [docs/architecture.md](docs/architecture.md) for the
  security trade-off vs. the prefix-list allow-list.

---

## 4. Build the container image (CodeBuild → ECR)

Local Docker is **not** assumed. The authoritative build path builds the
multi-stage image (the `node:22-slim` web build + the `python:3.12-slim`
runtime) **remotely with AWS CodeBuild** and pushes it to ECR. It is idempotent
(creates the ECR repo, S3 source bucket, IAM role, and CodeBuild project only if
missing) and **waits for `SUCCEEDED`**:

```bash
export IMAGE_TAG=$(git rev-parse --short HEAD)   # or any tag you choose
AWS_REGION=us-east-1 scripts/codebuild_image.sh
```

On success it prints the pushed image URI:
`<AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/ar-collections-nova-sonic:<IMAGE_TAG>`.
Capture that URI for the deploy:

```bash
export CONTAINER_IMAGE=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.us-east-1.amazonaws.com/ar-collections-nova-sonic:$IMAGE_TAG
```

> If a build fails, the script prints the `batch-get-builds` command to fetch
> logs. Fix and re-run; it is safe to re-run.

---

## 5. Deploy the CloudFormation stack (IaC)

`scripts/deploy.sh deploy` runs `aws cloudformation deploy` with all parameters.
The recommended live configuration fronts the HTTP-only ALB with CloudFront so
Twilio gets a trusted `wss://` endpoint with **no custom domain**:

```bash
export AWS_REGION=us-east-1
export STACK_NAME=ar-collections-nova-sonic
export IMAGE_TAG=$IMAGE_TAG            # from step 4 (deploy.sh composes the ECR URI)
export ENABLE_CLOUDFRONT=true
# VPC_ID / PUBLIC_SUBNET_IDS from step 3; the four TWILIO_*_ARN from step 2.
# Observability: emit the GA Strands OTEL spans to CloudWatch via the console exporter.
export OTEL_ENABLED=true
export OTEL_EXPORTER=console

scripts/deploy.sh deploy
```

Parameters passed to the stack:

| Parameter | Source / value |
| --- | --- |
| `ContainerImage` | the ECR image from step 4 |
| `VpcId` | `VPC_ID` |
| `PublicSubnetIds` | `PUBLIC_SUBNET_IDS` (≥ 2 AZs) |
| `TwilioAccountSidArn` … `TwilioVerifiedCallerIdArn` | the 4 Secrets Manager ARNs (step 2) |
| `EnableCloudFront` | `true` (CloudFront TLS front door) |
| `OtelEnabled` / `OtelExporter` | `true` / `console` (GA OTEL spans → CloudWatch) |
| `UseVpcOrigin` / `PrivateSubnetIds` / `NatGatewaySubnetId` | optional VPC-origin option (step 3) |
| `CertificateArn` / `PublicHostname` | optional custom-domain (ACM) alternative to CloudFront |
| `ExtractionModelId` | optional; defaults to `amazon.nova-lite-v1:0` |

> The `deploy.sh` wrapper reads these as environment variables: `VPC_ID`,
> `PUBLIC_SUBNET_IDS`, `TWILIO_*_ARN`, `ENABLE_CLOUDFRONT`, `OTEL_ENABLED`,
> `OTEL_EXPORTER`, and (optional) `USE_VPC_ORIGIN`, `PRIVATE_SUBNET_IDS`,
> `NAT_GATEWAY_SUBNET_ID`, `CERTIFICATE_ARN`, `PUBLIC_HOSTNAME`.

CloudFront takes ~5–15 min to deploy; the stack reaches
`CREATE_COMPLETE`/`UPDATE_COMPLETE` once live. Read the outputs:

```bash
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region us-east-1 \
  --query 'Stacks[0].Outputs' --output table
```

Key outputs: `CloudFrontDomainName` (the public URL), `DuesTableName`,
`CallsTableName`, `DashboardUrl`, `AlbDnsName`.

---

## 6. Seed the demo dues data

A fresh stack starts with an **empty** `DuesTable`, so the dashboard shows no
accounts until it is seeded. This was previously a manual step; it is now a
small idempotent script that writes the demo records (`ACC-1001`, `ACC-1002`),
parameterized by the table name from the stack output (never a hard-coded
physical name):

```bash
export DUES_TABLE=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='DuesTableName'].OutputValue|[0]" --output text)

AWS_REGION=us-east-1 scripts/seed_dues.py        # or: scripts/seed_dues.py --dues-table "$DUES_TABLE" --region us-east-1
```

It is safe to re-run (it uses `put_item`, insert-or-replace). Add `--dry-run` to
preview the records without writing.

---

## 7. Force a new ECS deployment

When the image tag changed, step 5 already rolls the service. If you rebuilt the
image under a **moving tag** (e.g. `latest`) or just want to roll the tasks after
seeding, force a new deployment and wait for the service to stabilize. The
cluster/service names are **discovered from the stack**, not hard-coded:

```bash
STACK_NAME=ar-collections-nova-sonic AWS_REGION=us-east-1 scripts/deploy.sh redeploy
```

(Equivalent raw commands: `aws ecs update-service --cluster <stack>-cluster
--service <service> --force-new-deployment --region us-east-1` then
`aws ecs wait services-stable ...`.)

---

## 8. Reach the dashboard and place a call

1. **Open the dashboard** at the CloudFront domain:

   ```bash
   CF=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region us-east-1 \
     --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue|[0]" --output text)
   echo "https://$CF/"
   curl -sS -o /dev/null -w 'healthz: %{http_code}\n' "https://$CF/healthz"   # expect 200
   ```

   The dashboard lists the seeded due records and past calls and shows per-call
   metrics, the chat transcript, the per-turn latency table, and the live event
   log.

2. **Place a call** — any of:

   - the dashboard **"Call now"** button on a due record;
   - the CLI: `scripts/place_call.py --account-id ACC-1001` (dry-run with
     `--dry-run`); or
   - the REST API:

     ```bash
     curl -X POST "https://$CF/api/calls" \
       -H 'content-type: application/json' -d '{"account_id":"ACC-1001"}'
     ```

   On a Twilio **trial** account the call/SMS can only reach the verified caller
   id, which the app targets automatically.

---

## Verify / observe

- **Health + config:** `GET /healthz` → `200`; `GET /api/config` shows the
  region and the OTEL markers (`otel_enabled`, `otel_exporter`).
- **Data:** `GET /api/dues` lists the seeded records; `GET /api/calls` lists
  calls.
- **Logs + OTEL spans:** `aws logs tail "/ecs/<stack>/app" --follow --region us-east-1`
  shows the per-call JSON lines and the GA Strands OTEL spans (session token
  usage, per-response native TTFT, tool calls).
- **WebSocket upgrade probe (optional):**
  `python scripts/ws_probe.py "$CF"` proves the `wss://<cloudfront-domain>/media`
  101 handshake end-to-end.

## Updating a running deployment

1. Commit changes and rebuild: `IMAGE_TAG=$(git rev-parse --short HEAD) scripts/codebuild_image.sh`.
2. Re-run `scripts/deploy.sh deploy` with the new `IMAGE_TAG` (updates
   `ContainerImage`), **or** if the tag is unchanged, `scripts/deploy.sh redeploy`.
3. Smoke-test `https://<cloudfront-domain>/healthz` and the `/api/*` endpoints.
   If a deploy regresses the app, roll back by deploying the prior image tag.

## Teardown

```bash
aws cloudformation delete-stack --stack-name ar-collections-nova-sonic --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name ar-collections-nova-sonic --region us-east-1
```

Also remove the Secrets Manager secrets, the ECR repository, and the CodeBuild
source S3 bucket / project / role if you no longer need them.
