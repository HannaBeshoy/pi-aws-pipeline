#!/usr/bin/env bash
# teardown.sh - delete everything the stack made (the Learner Lab budget is limited).
#   deploy/teardown.sh STACK
set -euo pipefail
STACK="${1:?usage: deploy/teardown.sh STACK}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
for b in "$STACK-$ACCOUNT-data" "$STACK-$ACCOUNT-artifacts"; do
  aws s3 rm "s3://$b" --recursive 2>/dev/null || true       # CloudFormation cannot delete a bucket that is not empty
done
aws cloudformation delete-stack --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --stack-name "$STACK"
aws s3 rb "s3://$STACK-$ACCOUNT-artifacts" 2>/dev/null || true
echo "deleted $STACK"
