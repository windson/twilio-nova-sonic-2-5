# Deployment guide

A reproducible, end-to-end runbook to deploy this AWS sample from scratch into
**your own AWS account**. Deployment is **pure CloudFormation**: a single
template (`infra/template.yaml`) provisions everything, **builds the container
image in-stack** (AWS CodeBuild), and **seeds the demo data in-stack** (a Lambda
custom resource). There are **no wrapper scripts** in the deploy path. A
customer deploys with one `aws cloudformation create-stack` command (plus a
`wait`).

All commands target **`us-east-1`**, where Nova Sonic 2.5, Amazon Bedrock
AgentCore Code Interpreter, and CloudFront VPC origins are all available. Set
`AWS_REGION=us-east-1` or pass `--region us-east-1` throughout.

> **Secrets.** Twilio credentials are stored in AWS Secrets Manager and
> referenced by **ARN**. No secret value is ever placed in the template, in a
> script, or in a parameter override passed to CloudFormation.

---

## 0. Overview — the shape of a deploy

```
prerequisites  ->  store Twilio secrets  ->  create-stack (ONE IaC command)
      ->  CloudFormation builds the image (CodeBuild) + seeds the dues
      ->  open the dashboard + place a call
```

The template is self-contained. On `create-stack` CloudFormation:

| In-stack resource | Replaces the old script | What it does |
| --- | --- | --- |
| `AppEcrRepo` (`AWS::ECR::Repository`) | `scripts/codebuild_image.sh` | Holds the built image. |
| `ImageBuildProject` (`AWS::CodeBuild::Project`) + `ImageBuildFn`/`ImageBuildTrigger` (Lambda custom resource) | `scripts/codebuild_image.sh` | Clones the **public** GitHub repo, runs `infra/buildspec.yml` to build and push the image to `AppEcrRepo`, and the custom resource **waits for the build to finish** before the ECS task definition is created (`DependsOn: ImageBuildTrigger`). |
| `SeedDuesFn`/`SeedDuesTrigger` (Lambda custom resource) | `scripts/seed_dues.py` | `PutItem`s the demo dues `ACC-1001` / `ACC-1002` into `DuesTable` on Create (idempotent). |
| the rest of the stack (ECS/ALB/CloudFront/DynamoDB/CloudWatch/CloudTrail) | `scripts/deploy.sh` | the service itself; `aws cloudformation create-stack`/`update-stack` is the deploy, no wrapper. |

`scripts/place_call.py` and `scripts/ws_probe.py` remain — they are operator
**test utilities**, not deploy steps.

---

## 1. Prerequisites

1. **An AWS account** with permissions to create ECR, CodeBuild, Lambda, IAM,
   ECS, ELBv2, DynamoDB, CloudWatch, CloudFront, CloudTrail, and Secrets Manager
   resources, and the **AWS CLI v2** configured for `us-east-1`:

   ```bash
   aws configure            # or use an SSO/role profile
   aws sts get-caller-identity --region us-east-1   # confirm identity
   ```

2. **Python 3.12** (only for the local tooling / test utilities — **not** needed
   to deploy, since the image is built in-stack). **Not 3.13** — the audio
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
value into the template, a command, or shell history you commit.

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

Capture the four ARNs (shape shown with placeholders — `<region>` /
`<AWS_ACCOUNT_ID>` are filled by your account):

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

**CloudFront VPC origin (activated origin posture).** The reference stack runs
the ALB **internal** (no public IP, reachable only via CloudFront's VPC origin
ENI) with `UseVpcOrigin=true`. The private subnets are provisioned by the
template itself (IaC) when `PrivateSubnetIds` is left blank, so you do **not**
need to pre-create them; only a public subnet for NAT egress is required:

```bash
export NAT_GATEWAY_SUBNET_ID=<public-subnet-for-nat>   # NAT egress for private tasks
```

Notes for the VPC-origin posture:
- The private subnets that host the internal ALB and the Fargate tasks are
  provisioned by the template when `PrivateSubnetIds` is left blank (no manual
  subnet creation needed); the NAT subnet is still a **public** subnet. The
  subnets in use are surfaced by the `ProvisionedPrivateSubnetIds` stack output.
- The first VPC origin in an account triggers a **one-time** auto-provisioning of
  the `AWSServiceRoleForCloudFrontVPCOrigins` service-linked role (no action
  needed; just be aware it appears).
- Switching an **existing** stack from a public ALB to an internal one
  **replaces** the load balancer (new DNS/ARN — not zero-downtime), but the
  CloudFront **domain is unchanged** across the replacement. Prefer a fresh
  stack or a planned window. See [docs/architecture.md](docs/architecture.md) for
  the security trade-off vs. the prefix-list allow-list.

---

## 4. Deploy the whole stack with ONE CloudFormation command

The template is **under the 51,200-byte inline limit**, so you pass it directly
with `--template-body file://infra/template.yaml` — **no S3 upload and no
wrapper script**. `create-stack` provisions the infrastructure, the in-stack
CodeBuild project builds and pushes the image, and the Lambda custom resources
wait for the build and seed the demo dues, all in one operation.

The recommended live configuration fronts the HTTP-only internal ALB with the
CloudFront VPC origin so Twilio gets a trusted `wss://` endpoint with **no
custom domain**:

```bash
export AWS_REGION=us-east-1
export STACK_NAME=ar-collections-nova-sonic

aws cloudformation create-stack \
  --region us-east-1 \
  --stack-name "$STACK_NAME" \
  --template-body file://infra/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=VpcId,ParameterValue="$VPC_ID" \
    ParameterKey=PublicSubnetIds,ParameterValue=\"$PUBLIC_SUBNET_IDS\" \
    ParameterKey=TwilioAccountSidArn,ParameterValue="$TWILIO_ACCOUNT_SID_ARN" \
    ParameterKey=TwilioAuthTokenArn,ParameterValue="$TWILIO_AUTH_TOKEN_ARN" \
    ParameterKey=TwilioFromNumberArn,ParameterValue="$TWILIO_FROM_NUMBER_ARN" \
    ParameterKey=TwilioVerifiedCallerIdArn,ParameterValue="$TWILIO_VERIFIED_CALLER_ID_ARN" \
    ParameterKey=EnableCloudFront,ParameterValue=true \
    ParameterKey=UseVpcOrigin,ParameterValue=true \
    ParameterKey=NatGatewaySubnetId,ParameterValue="$NAT_GATEWAY_SUBNET_ID"

aws cloudformation wait stack-create-complete \
  --region us-east-1 --stack-name "$STACK_NAME"
```

> Use `update-stack` (same parameters) to apply changes to an existing stack.
> `CAPABILITY_NAMED_IAM` is required because the stack creates named IAM roles.
> CloudFront takes ~5–15 min to deploy; the first in-stack image build adds a
> few minutes. The stack reaches `CREATE_COMPLETE` once everything is live.

### Parameters

| Parameter | Default | Notes |
| --- | --- | --- |
| `VpcId` | — | target VPC |
| `PublicSubnetIds` | — | ≥ 2 AZs |
| `TwilioAccountSidArn` … `TwilioVerifiedCallerIdArn` | — | the 4 Secrets Manager ARNs (step 2) |
| `GitHubRepoUrl` | `https://github.com/windson/twilio-nova-sonic-2-5.git` | **public** repo CodeBuild clones for the in-stack build |
| `SourceVersion` | `main` | git branch/tag/sha CodeBuild builds from |
| `ImageTag` | `latest` | tag the in-stack build pushes and the task consumes |
| `ContainerImage` | `""` | optional explicit ECR image URI override (blank = use the in-stack built image) |
| `EnableCloudFront` | `false` | CloudFront TLS front door |
| `UseVpcOrigin` | `false` | internal ALB behind a CloudFront VPC origin (needs `EnableCloudFront=true` + `NatGatewaySubnetId`) |
| `PrivateSubnetIds` | `""` | blank = stack provisions them |
| `NatGatewaySubnetId` | `""` | public subnet for NAT egress when `UseVpcOrigin=true` |
| `CertificateArn` / `PublicHostname` | `""` | optional custom-domain (ACM) alternative to CloudFront |
| `OtelEnabled` / `OtelExporter` | `true` / `console` | GA OTEL spans → stdout → CloudWatch (no new IAM/infra) |
| `ExtractionModelId` | `amazon.nova-lite-v1:0` | text model for post-call extraction |

Read the outputs:

```bash
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region us-east-1 \
  --query 'Stacks[0].Outputs' --output table
```

Key outputs: `CloudFrontDomainName` (the public URL), `ContainerImageUri`,
`EcrRepositoryUri`, `DuesTableName`, `CallsTableName`, `DashboardUrl`,
`AlbDnsName`.

### How the in-stack image build works

- `ImageBuildProject` is an `AWS::CodeBuild::Project` whose `Source` is the
  **public** GitHub repo (`GitHubRepoUrl`, at `SourceVersion`). CodeBuild clones
  it with no credentials (the repo is public) and runs `infra/buildspec.yml`,
  which `docker build`s the multi-stage image (the `node:22-slim` web build +
  the `python:3.12-slim` runtime) and pushes it to the stack's `AppEcrRepo` at
  `:ImageTag`.
- `ImageBuildFn` (an inline Lambda) is invoked by the `ImageBuildTrigger`
  custom resource on Create/Update. It calls `codebuild:StartBuild`, then polls
  `codebuild:BatchGetBuilds` (self-re-invoking while `IN_PROGRESS`) and signals
  CloudFormation only when the build `SUCCEEDED`/`FAILED`.
- The `TaskDefinition` has `DependsOn: ImageBuildTrigger`, so the ECS task is
  only created after a successful image build, and it consumes
  `${AppEcrRepo.RepositoryUri}:${ImageTag}` (unless you set `ContainerImage`).

> **Local Docker is not required or used.** The broken local container runtime
> in some environments is exactly why the build runs remotely in CodeBuild.

### How the demo-dues seeding works

`SeedDuesFn` (an inline Lambda, triggered by `SeedDuesTrigger`) `PutItem`s the
two demo records `ACC-1001` (Jordan Rivera) and `ACC-1002` (Pavan Navule) into
`DuesTable` with the same schema the app uses, on stack Create. It is
idempotent (`put_item` insert-or-replace) and a no-op on Update/Delete, and its
IAM role is scoped to `dynamodb:PutItem` on `DuesTable` only.

---

## 5. Force a fresh image build / roll the service

To rebuild the image and roll the service, **update the stack** with a new
`ImageTag` (and/or `SourceVersion`):

```bash
aws cloudformation update-stack \
  --region us-east-1 --stack-name "$STACK_NAME" \
  --template-body file://infra/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=ImageTag,ParameterValue=$(git rev-parse --short HEAD) \
    ParameterKey=SourceVersion,ParameterValue=main \
    --use-previous-value ...   # reuse the other parameters

aws cloudformation wait stack-update-complete --region us-east-1 --stack-name "$STACK_NAME"
```

Changing `ImageTag` re-triggers the in-stack CodeBuild build (the custom
resource property changes) and updates the ECS task definition to the new image,
so CloudFormation rolls the service. (In practice, pass `--use-previous-value`
for every parameter you are not changing.)

---

## 6. Reach the dashboard and place a call

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
   - the CLI: `.venv/bin/python scripts/place_call.py --account-id ACC-1001`
     (dry-run with `--dry-run`; needs the project venv since it imports
     `app.config`/`app.store`); or
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
- **Image build logs:** the in-stack build logs to the
  `/codebuild/<stack>-image-build` CloudWatch log group; the custom-resource
  Lambdas log to their own `/aws/lambda/...` groups.
- **App logs + OTEL spans:** `aws logs tail "/ecs/<stack>/app" --follow --region us-east-1`
  shows the per-call JSON lines and the GA Strands OTEL spans (session token
  usage, per-response native TTFT, tool calls).
- **WebSocket upgrade probe (optional):**
  `.venv/bin/python scripts/ws_probe.py "$CF"` proves the
  `wss://<cloudfront-domain>/media` 101 handshake end-to-end (uses the
  `websockets` dependency from the project venv).

## Validate the template before deploying (optional)

```bash
.venv/bin/cfn-lint infra/template.yaml          # expect 0 findings
wc -c infra/template.yaml                        # expect < 51200 bytes
aws cloudformation validate-template --region us-east-1 \
  --template-body file://infra/template.yaml
```

## Teardown

```bash
aws cloudformation delete-stack --stack-name ar-collections-nova-sonic --region us-east-1
aws cloudformation wait stack-delete-complete --stack-name ar-collections-nova-sonic --region us-east-1
```

Also remove the Secrets Manager secrets if you no longer need them. The ECR
repository, CodeBuild project, and custom-resource Lambdas/roles are part of the
stack and are deleted with it (the ECR repo is emptied by CloudFormation only if
empty; delete remaining images first if deletion is blocked).
