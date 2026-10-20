#!/usr/bin/env bash
#
# Operator convenience wrapper for building and deploying the AR Collections
# Nova Sonic 2.5 Agent to AWS. This script is for a human operator to run from
# a shell; it is NOT executed by the test suite and performs live AWS mutations
# only when you explicitly invoke the `deploy` subcommand.
#
# Prerequisites:
#   - aws-cli configured with a profile that has permissions for ECR, ECS,
#     ELBv2, DynamoDB, CloudWatch, CloudTrail, S3, and IAM in your AWS account
#     (region us-east-1).
#   - The container image built and pushed to ECR. The RECOMMENDED path is the
#     remote build via scripts/codebuild_image.sh (CodeBuild -> ECR), which does
#     not require a local Docker daemon. The `build`/`push` subcommands below
#     use a local Docker daemon and are optional fallbacks.
#   - cfn-lint installed (pip install cfn-lint) for the `lint` subcommand.
#   - Twilio credentials already stored in AWS Secrets Manager / SSM Parameter
#     Store; pass their ARNs to `deploy` (NEVER inline secret values).
#
# Usage:
#   scripts/deploy.sh lint       # cfn-lint + validate-template (no mutation)
#   scripts/deploy.sh build      # optional local Docker build (prefer CodeBuild)
#   scripts/deploy.sh push       # optional local push to ECR
#   scripts/deploy.sh deploy     # aws cloudformation deploy with parameters
#   scripts/deploy.sh redeploy   # force a new ECS deployment + wait stable
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-ar-collections-nova-sonic}"
ECR_REPO="${ECR_REPO:-ar-collections-nova-sonic}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_ROOT}/infra/template.yaml"

account_id() {
  aws sts get-caller-identity --query Account --output text
}

image_uri() {
  echo "$(account_id).dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
}

cmd_lint() {
  echo "==> Validating CloudFormation template with cfn-lint"
  cfn-lint "${TEMPLATE}"
  echo "==> Validating template with CloudFormation service"
  aws cloudformation validate-template \
    --region "${AWS_REGION}" \
    --template-body "file://${TEMPLATE}" >/dev/null
  echo "OK"
}

cmd_build() {
  # Optional local-Docker fallback. The RECOMMENDED path is the remote build:
  #   IMAGE_TAG=<git-sha> scripts/codebuild_image.sh
  # which builds via AWS CodeBuild and pushes to ECR with no local Docker daemon.
  echo "==> Building Docker image locally (fallback; prefer scripts/codebuild_image.sh)"
  docker build -t "${ECR_REPO}:${IMAGE_TAG}" "${REPO_ROOT}"
}

cmd_push() {
  local uri acct
  acct="$(account_id)"
  uri="$(image_uri)"
  echo "==> Ensuring ECR repository exists: ${ECR_REPO}"
  aws ecr describe-repositories --region "${AWS_REGION}" --repository-names "${ECR_REPO}" \
    >/dev/null 2>&1 || aws ecr create-repository --region "${AWS_REGION}" --repository-name "${ECR_REPO}" >/dev/null
  echo "==> Logging in to ECR"
  aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${acct}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  echo "==> Tagging and pushing ${uri}"
  docker tag "${ECR_REPO}:${IMAGE_TAG}" "${uri}"
  docker push "${uri}"
}

cmd_deploy() {
  : "${VPC_ID:?Set VPC_ID to the target VPC id}"
  : "${PUBLIC_SUBNET_IDS:?Set PUBLIC_SUBNET_IDS (comma-separated subnet ids in >=2 AZs)}"
  : "${TWILIO_ACCOUNT_SID_ARN:?Set TWILIO_ACCOUNT_SID_ARN (Secrets Manager/SSM ARN)}"
  : "${TWILIO_AUTH_TOKEN_ARN:?Set TWILIO_AUTH_TOKEN_ARN}"
  : "${TWILIO_FROM_NUMBER_ARN:?Set TWILIO_FROM_NUMBER_ARN}"
  : "${TWILIO_VERIFIED_CALLER_ID_ARN:?Set TWILIO_VERIFIED_CALLER_ID_ARN}"

  local uri
  uri="$(image_uri)"

  # TLS options - two mutually-exclusive paths to a wss://-reachable endpoint
  # (Twilio Media Streams only connect over TLS, and the raw ALB DNS has no
  # certificate):
  #
  #   Option 1 (CloudFront, no domain needed): set ENABLE_CLOUDFRONT=true to put
  #   an Amazon CloudFront distribution in front of the HTTP:80 ALB. CloudFront
  #   supplies a FREE AWS-managed *.cloudfront.net TLS hostname and
  #   PUBLIC_BASE_URL resolves to it, so TwiML emits
  #   wss://<dist>.cloudfront.net/media. No ACM cert or custom DNS required.
  #
  #   Custom-domain path (ACM): set CERTIFICATE_ARN (ACM) and PUBLIC_HOSTNAME
  #   (a DNS name that points at the ALB and that the cert covers) to add an
  #   HTTPS:443 listener on the ALB and make PUBLIC_BASE_URL the TLS hostname.
  #
  # ENABLE_CLOUDFRONT defaults to "false" so existing behavior is preserved.
  local tls_overrides=()
  tls_overrides+=("EnableCloudFront=${ENABLE_CLOUDFRONT:-false}")
  if [[ -n "${CERTIFICATE_ARN:-}" ]]; then
    tls_overrides+=("CertificateArn=${CERTIFICATE_ARN}")
  fi
  if [[ -n "${PUBLIC_HOSTNAME:-}" ]]; then
    tls_overrides+=("PublicHostname=${PUBLIC_HOSTNAME}")
  fi

  # CloudFront VPC origin (Option 2) - network-level origin isolation. When
  # USE_VPC_ORIGIN=true (with ENABLE_CLOUDFRONT=true) the ALB becomes INTERNAL in
  # PRIVATE_SUBNET_IDS with no public IP, tasks run private with NAT egress via
  # NAT_GATEWAY_SUBNET_ID, and CloudFront reaches the origin through an
  # AWS::CloudFront::VpcOrigin ENI. Defaults to "false" (public ALB + prefix-list
  # allow-list), so existing behavior is preserved. NOTE: flipping this on an
  # existing stack REPLACES the load balancer (new DNS/ARN, not zero-downtime).
  tls_overrides+=("UseVpcOrigin=${USE_VPC_ORIGIN:-false}")
  if [[ -n "${PRIVATE_SUBNET_IDS:-}" ]]; then
    tls_overrides+=("PrivateSubnetIds=${PRIVATE_SUBNET_IDS}")
  fi
  if [[ -n "${NAT_GATEWAY_SUBNET_ID:-}" ]]; then
    tls_overrides+=("NatGatewaySubnetId=${NAT_GATEWAY_SUBNET_ID}")
  fi

  # GA Strands OTEL telemetry passthrough (non-secret). Only override the stack
  # parameters when the env vars are set, so the template defaults
  # (OtelEnabled=true, OtelExporter=console) apply otherwise.
  local otel_overrides=()
  if [[ -n "${OTEL_ENABLED:-}" ]]; then
    otel_overrides+=("OtelEnabled=${OTEL_ENABLED}")
  fi
  if [[ -n "${OTEL_EXPORTER:-}" ]]; then
    otel_overrides+=("OtelExporter=${OTEL_EXPORTER}")
  fi
  if [[ -n "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" ]]; then
    otel_overrides+=("OtelExporterOtlpEndpoint=${OTEL_EXPORTER_OTLP_ENDPOINT}")
  fi
  if [[ "${ENABLE_CLOUDFRONT:-false}" != "true" && -z "${CERTIFICATE_ARN:-}" && -z "${PUBLIC_HOSTNAME:-}" ]]; then
    echo "==> WARNING: no ENABLE_CLOUDFRONT=true and no CERTIFICATE_ARN/" \
         "PUBLIC_HOSTNAME set - deploying an HTTP:80-only ALB. Twilio (wss://)" \
         "will NOT connect to this; enable CloudFront (Option 1) or set both" \
         "ACM values for a live call." >&2
  fi

  echo "==> Deploying stack ${STACK_NAME} with image ${uri}"
  aws cloudformation deploy \
    --region "${AWS_REGION}" \
    --stack-name "${STACK_NAME}" \
    --template-file "${TEMPLATE}" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
      ContainerImage="${uri}" \
      VpcId="${VPC_ID}" \
      PublicSubnetIds="${PUBLIC_SUBNET_IDS}" \
      TwilioAccountSidArn="${TWILIO_ACCOUNT_SID_ARN}" \
      TwilioAuthTokenArn="${TWILIO_AUTH_TOKEN_ARN}" \
      TwilioFromNumberArn="${TWILIO_FROM_NUMBER_ARN}" \
      TwilioVerifiedCallerIdArn="${TWILIO_VERIFIED_CALLER_ID_ARN}" \
      "${tls_overrides[@]}" \
      ${otel_overrides[@]+"${otel_overrides[@]}"}

  echo "==> Stack outputs (PUBLIC_BASE_URL is the TLS hostname when set, else ALB DNS)"
  aws cloudformation describe-stacks \
    --region "${AWS_REGION}" \
    --stack-name "${STACK_NAME}" \
    --query 'Stacks[0].Outputs' --output table
}

# Force ECS to pull a freshly-pushed image tag and wait for the service to
# stabilize. Run this after pushing a new image when the ContainerImage tag did
# not change (e.g. a moving tag), or any time you want to roll the tasks. The
# cluster and service names are DISCOVERED from the stack, never hard-coded.
cmd_redeploy() {
  local cluster="${STACK_NAME}-cluster"
  local service
  service="$(aws ecs list-services --region "${AWS_REGION}" --cluster "${cluster}" \
    --query 'serviceArns[0]' --output text)"
  if [[ -z "${service}" || "${service}" == "None" ]]; then
    echo "ERROR: could not find a service in cluster ${cluster}" >&2
    exit 1
  fi
  echo "==> Forcing new deployment of ${service} in ${cluster}"
  aws ecs update-service --region "${AWS_REGION}" --cluster "${cluster}" \
    --service "${service}" --force-new-deployment >/dev/null
  echo "==> Waiting for the service to become stable (this can take a few minutes)"
  aws ecs wait services-stable --region "${AWS_REGION}" --cluster "${cluster}" --services "${service}"
  echo "OK"
}

main() {
  local sub="${1:-}"
  case "${sub}" in
    lint) cmd_lint ;;
    build) cmd_build ;;
    push) cmd_push ;;
    deploy) cmd_deploy ;;
    redeploy) cmd_redeploy ;;
    all) cmd_lint; cmd_build; cmd_push; cmd_deploy ;;
    *)
      echo "Usage: $0 {lint|build|push|deploy|redeploy|all}" >&2
      exit 2
      ;;
  esac
}

main "$@"
