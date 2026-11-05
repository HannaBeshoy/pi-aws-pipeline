#!/usr/bin/env bash
# deploy.sh - create or update the stack.
#   deploy/deploy.sh STACK [--academy] [--email you@example.com] [--device pi-01]
# --academy: AWS Academy Learner Lab, which may not create IAM roles or users: the Lambda
# functions use the lab's existing LabRole instead of the template's least-privilege roles.
# Needs the AWS CLI v2, configured (in the Learner Lab: copy "AWS Details" -> ~/.aws/credentials).
set -euo pipefail
cd "$(dirname "$0")/.."
STACK="${1:?usage: deploy/deploy.sh STACK [--academy] [--email ADDR] [--device ID]}"; shift
ACADEMY=0; EMAIL=""; DEVICE=pi-01
while [ $# -gt 0 ]; do
  case "$1" in
    --academy) ACADEMY=1; shift ;;
    --email) EMAIL="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ART="$STACK-$ACCOUNT-artifacts"
aws s3api head-bucket --bucket "$ART" 2>/dev/null || {
  if [ "$REGION" = us-east-1 ]; then aws s3api create-bucket --bucket "$ART" >/dev/null
  else aws s3api create-bucket --bucket "$ART" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null; fi
  aws s3api put-public-access-block --bucket "$ART" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
}
mkdir -p build
aws cloudformation package --template-file infra/template.yaml --s3-bucket "$ART" --output-template-file build/packaged.yaml >/dev/null
PARAMS=("DeviceId=$DEVICE" "AlarmEmail=$EMAIL")
[ $ACADEMY = 1 ] && PARAMS+=("LabRoleArn=arn:aws:iam::$ACCOUNT:role/LabRole")
aws cloudformation deploy --stack-name "$STACK" --template-file build/packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --parameter-overrides "${PARAMS[@]}"
aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs" --output table
