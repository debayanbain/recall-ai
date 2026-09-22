#!/bin/bash
# Reads /recallai/app/* from SSM and writes the Kubernetes Secret "recallai-env".
set -euo pipefail
PREFIX="/recallai/app"; REGION="ap-south-1"
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
aws ssm get-parameters-by-path --path "$PREFIX" --with-decryption \
  --region "$REGION" --output json \
  | python3 -c '
import json, sys
for p in json.load(sys.stdin)["Parameters"]:
    print(p["Name"].rsplit("/", 1)[1] + "=" + p["Value"])
' > "$TMP"
kubectl create secret generic recallai-env -n recallai \
  --from-env-file="$TMP" --dry-run=client -o yaml | kubectl apply -f -
echo "Secret updated with $(wc -l < "$TMP" | tr -d ' ') keys"
