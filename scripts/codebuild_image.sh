#!/usr/bin/env bash
#
# Build the app container image REMOTELY with AWS CodeBuild and push it to ECR.
#
# Why: some environments (including the CI sandbox this was first run in) have a
# broken local container runtime (`docker build` fails with a conmon error), so
# the image cannot be built locally. CodeBuild provides its own Docker daemon.
# This script uses an S3-zipped copy of the repo as the CodeBuild source, which
# avoids any Git/GitHub auth wiring.
#
# It is idempotent: the ECR repo, S3 bucket, IAM role, and CodeBuild project are
# created only if missing. Safe to re-run to rebuild/push a new tag.
#
# Prereqs: aws-cli with permissions for ECR, S3, IAM, and CodeBuild.
#
# Usage:
#   scripts/codebuild_image.sh            # builds tag "latest"
#   IMAGE_TAG=abc123 scripts/codebuild_image.sh
#
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-ar-collections-nova-sonic}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
PROJECT_NAME="${PROJECT_NAME:-ar-collections-nova-sonic-build}"
ROLE_NAME="${ROLE_NAME:-ar-collections-nova-sonic-codebuild-role}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --region "${AWS_REGION}" --query Account --output text)"
SOURCE_BUCKET="${SOURCE_BUCKET:-ar-collections-nova-sonic-build-${ACCOUNT_ID}-${AWS_REGION}}"
REPO_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "==> Account ${ACCOUNT_ID}, region ${AWS_REGION}"
echo "==> Target image: ${REPO_URI}:${IMAGE_TAG}"

# --- ECR repo ---------------------------------------------------------------
aws ecr describe-repositories --region "${AWS_REGION}" --repository-names "${ECR_REPO}" >/dev/null 2>&1 \
  || aws ecr create-repository --region "${AWS_REGION}" --repository-name "${ECR_REPO}" \
       --image-scanning-configuration scanOnPush=true >/dev/null
echo "==> ECR repo ready: ${ECR_REPO}"

# --- S3 source bucket -------------------------------------------------------
if ! aws s3api head-bucket --bucket "${SOURCE_BUCKET}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  if [[ "${AWS_REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${SOURCE_BUCKET}" --region "${AWS_REGION}" >/dev/null
  else
    aws s3api create-bucket --bucket "${SOURCE_BUCKET}" --region "${AWS_REGION}" \
      --create-bucket-configuration LocationConstraint="${AWS_REGION}" >/dev/null
  fi
fi
echo "==> S3 source bucket ready: ${SOURCE_BUCKET}"

# --- IAM role for CodeBuild -------------------------------------------------
if ! aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{"Effect":"Allow","Principal":{"Service":"codebuild.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' >/dev/null
  aws iam put-role-policy --role-name "${ROLE_NAME}" --policy-name inline \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"*\"},
        {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:GetObjectVersion\",\"s3:GetBucketLocation\"],\"Resource\":[\"arn:aws:s3:::${SOURCE_BUCKET}\",\"arn:aws:s3:::${SOURCE_BUCKET}/*\"]},
        {\"Effect\":\"Allow\",\"Action\":[\"ecr:GetAuthorizationToken\"],\"Resource\":\"*\"},
        {\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchCheckLayerAvailability\",\"ecr:CompleteLayerUpload\",\"ecr:InitiateLayerUpload\",\"ecr:PutImage\",\"ecr:UploadLayerPart\",\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\"],\"Resource\":\"arn:aws:ecr:${AWS_REGION}:${ACCOUNT_ID}:repository/${ECR_REPO}\"}
      ]
    }" >/dev/null
  echo "==> Created IAM role ${ROLE_NAME}; waiting for propagation"
  sleep 15
fi
ROLE_ARN="$(aws iam get-role --role-name "${ROLE_NAME}" --query 'Role.Arn' --output text)"
echo "==> CodeBuild role: ${ROLE_ARN}"

# --- CodeBuild project ------------------------------------------------------
ENV_JSON="{
  \"type\": \"LINUX_CONTAINER\",
  \"image\": \"aws/codebuild/amazonlinux2-x86_64-standard:5.0\",
  \"computeType\": \"BUILD_GENERAL1_SMALL\",
  \"privilegedMode\": true,
  \"environmentVariables\": [
    {\"name\": \"AWS_ACCOUNT_ID\", \"value\": \"${ACCOUNT_ID}\"},
    {\"name\": \"ECR_REPO\", \"value\": \"${ECR_REPO}\"},
    {\"name\": \"IMAGE_TAG\", \"value\": \"${IMAGE_TAG}\"}
  ]
}"
SOURCE_JSON="{\"type\": \"S3\", \"location\": \"${SOURCE_BUCKET}/source.zip\", \"buildspec\": \"infra/buildspec.yml\"}"

if aws codebuild batch-get-projects --region "${AWS_REGION}" --names "${PROJECT_NAME}" \
     --query 'projects[0].name' --output text 2>/dev/null | grep -q "${PROJECT_NAME}"; then
  aws codebuild update-project --region "${AWS_REGION}" --name "${PROJECT_NAME}" \
    --source "${SOURCE_JSON}" --artifacts '{"type":"NO_ARTIFACTS"}' \
    --environment "${ENV_JSON}" --service-role "${ROLE_ARN}" >/dev/null
else
  aws codebuild create-project --region "${AWS_REGION}" --name "${PROJECT_NAME}" \
    --source "${SOURCE_JSON}" --artifacts '{"type":"NO_ARTIFACTS"}' \
    --environment "${ENV_JSON}" --service-role "${ROLE_ARN}" >/dev/null
fi
echo "==> CodeBuild project ready: ${PROJECT_NAME}"

# --- Zip + upload the source ------------------------------------------------
TMP_ZIP="$(mktemp /tmp/ar-collections-src-XXXX.zip)"
rm -f "${TMP_ZIP}"
( cd "${REPO_ROOT}" && zip -r -q "${TMP_ZIP}" . \
    -x '.git/*' -x '.venv/*' -x '.pytest_cache/*' -x '**/__pycache__/*' )
aws s3 cp "${TMP_ZIP}" "s3://${SOURCE_BUCKET}/source.zip" --region "${AWS_REGION}" >/dev/null
echo "==> Uploaded source.zip to s3://${SOURCE_BUCKET}/source.zip"

# --- Start build + wait -----------------------------------------------------
BUILD_ID="$(aws codebuild start-build --region "${AWS_REGION}" --project-name "${PROJECT_NAME}" \
  --environment-variables-override "name=IMAGE_TAG,value=${IMAGE_TAG},type=PLAINTEXT" \
  --query 'build.id' --output text)"
echo "==> Started build ${BUILD_ID}; polling for completion..."

while true; do
  STATUS="$(aws codebuild batch-get-builds --region "${AWS_REGION}" --ids "${BUILD_ID}" \
    --query 'builds[0].buildStatus' --output text)"
  if [[ "${STATUS}" == "IN_PROGRESS" ]]; then sleep 15; continue; fi
  echo "==> Build ${BUILD_ID} finished: ${STATUS}"
  break
done

if [[ "${STATUS}" != "SUCCEEDED" ]]; then
  echo "!! Build failed. Fetch logs with: aws codebuild batch-get-builds --ids ${BUILD_ID} --region ${AWS_REGION}" >&2
  exit 1
fi

echo "==> Image pushed: ${REPO_URI}:${IMAGE_TAG}"
