#!/bin/bash
# Usage: scripts/env-to-ssm.sh .env.production
set -euo pipefail
FILE="$1"; PREFIX="/recallai/app"; REGION="ap-south-1"
while IFS= read -r line || [ -n "$line" ]; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
  key="${line%%=*}"; value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  [[ -z "$value" ]] && { echo "skip $key (empty)"; continue; }
  aws ssm put-parameter --region "$REGION" --name "$PREFIX/$key" \
    --type SecureString --value "$value" --overwrite >/dev/null
  echo "saved $key"
done < "$FILE"
