#!/usr/bin/env bash
#
# Operator convenience wrapper for building and deploying the AR Collections
# Nova Sonic 2.5 Agent to AWS. This script is for a human operator to run from
# a shell; it is NOT executed by the test suite and performs live AWS mutations
# only when you explicitly invoke the `deploy` subcommand.
#
# Prerequisites:
#   - aws-cli configured with a profile that has permissions for ECR, ECS,
#     ELBv2, DynamoDB, CloudWatch, CloudTrail, S3, and IAM (account 920487201358,
#     region us-east-1).
#   - Docker available locally to build the image.
#   - cfn-lint installed (pip install cfn-lint) for the `lint` subcommand.
#   - Twilio credentials already stored in AWS Secrets Manager / SSM Parameter
#     Store; pass their ARNs to `deploy` (NEVER inline secret values).
#
# Usage:
#   scripts/deploy.sh lint
#   scripts/deploy.sh build
#   scripts/deploy.sh push
#   scripts/deploy.sh deploy
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
  echo "==> Building Docker image"
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

  # Optional TLS overrides. For a LIVE Twilio call both are required: Twilio
  # Media Streams only connect over wss:// (TLS), and the raw ALB DNS has no
  # certificate. Set CERTIFICATE_ARN (ACM) and PUBLIC_HOSTNAME (a DNS name that
  # points at the ALB and that the cert covers) to add an HTTPS:443 listener
  # and make PUBLIC_BASE_URL the TLS hostname.
  local tls_overrides=()
  if [[ -n "${CERTIFICATE_ARN:-}" ]]; then
    tls_overrides+=("CertificateArn=${CERTIFICATE_ARN}")
  fi
  if [[ -n "${PUBLIC_HOSTNAME:-}" ]]; then
    tls_overrides+=("PublicHostname=${PUBLIC_HOSTNAME}")
  fi
  if [[ ${#tls_overrides[@]} -eq 0 ]]; then
    echo "==> WARNING: no CERTIFICATE_ARN/PUBLIC_HOSTNAME set - deploying an" \
         "HTTP:80-only ALB. Twilio (wss://) will NOT connect to this; set both" \
         "for a live call." >&2
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
      "${tls_overrides[@]}"

  echo "==> Stack outputs (PUBLIC_BASE_URL is the TLS hostname when set, else ALB DNS)"
  aws cloudformation describe-stacks \
    --region "${AWS_REGION}" \
    --stack-name "${STACK_NAME}" \
    --query 'Stacks[0].Outputs' --output table
}

main() {
  local sub="${1:-}"
  case "${sub}" in
    lint) cmd_lint ;;
    build) cmd_build ;;
    push) cmd_push ;;
    deploy) cmd_deploy ;;
    all) cmd_lint; cmd_build; cmd_push; cmd_deploy ;;
    *)
      echo "Usage: $0 {lint|build|push|deploy|all}" >&2
      exit 2
      ;;
  esac
}

main "$@"
