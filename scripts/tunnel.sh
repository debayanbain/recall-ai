#!/bin/bash
# Keeps the SSM tunnel to the K3s API open. Ctrl+C twice to stop.
# SSM sessions idle-timeout after ~20 minutes; this reconnects.
ID=i-0d405675f36d126a8
while true; do
  aws ssm start-session --target "$ID" --region ap-south-1 \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["6443"],"localPortNumber":["16443"]}'
  echo "tunnel closed, reconnecting in 3s..."
  sleep 3
done
