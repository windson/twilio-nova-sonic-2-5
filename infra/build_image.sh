#!/usr/bin/env bash
# Invoked by the trusted template bootstrap after manifest authentication.
set -euo pipefail
bash infra/verify_source.sh
revision=$SOURCE_REVISION
repo_uri="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_DEFAULT_REGION}.amazonaws.com/${ECR_REPO}"
aws ecr get-login-password --region "$AWS_DEFAULT_REGION" |
  docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_DEFAULT_REGION}.amazonaws.com"
docker build --build-arg "BUILD_REVISION=$revision" -t "${repo_uri}:${IMAGE_TAG}" .
docker push "${repo_uri}:${IMAGE_TAG}"
printf 'Pushed image for verified Git commit %s\n' "$revision"
