#!/usr/bin/env bash
# Mint the kubeconfig GitHub Actions deploys with, and store it in SSM.
#
# Run this ONCE, from a laptop with admin kubectl access (scripts/tunnel.sh open
# in another terminal). CI can read the parameter and cannot write it -- the
# deploy role has no ssm:PutParameter anywhere.
#
#   ./scripts/cicd-kubeconfig.sh
#
# The credential inside is the github-deploy ServiceAccount token from
# k8s/70-deploy-rbac.yaml: namespaced to recallai, no secrets verbs, no
# cluster-scoped rights. It is never printed -- it goes from kubectl to a
# 0600 temp file to Parameter Store and nowhere else.
set -euo pipefail
umask 077

REGION="${AWS_REGION:-ap-south-1}"
PROJECT="${PROJECT:-recallai}"
NS="${NS:-recallai}"
SA="github-deploy"
PARAM="/${PROJECT}/cicd/kubeconfig"

# The address the RUNNER will use. The runner forwards the node's 6443 to its own
# 6443; your local tunnel's port is irrelevant to the file being written.
KUBE_SERVER="${KUBE_SERVER:-https://127.0.0.1:6443}"

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v aws >/dev/null || { echo "aws cli not found" >&2; exit 1; }

echo "==> applying k8s/70-deploy-rbac.yaml"
kubectl apply -f "$(dirname "$0")/../k8s/70-deploy-rbac.yaml"

echo "==> waiting for the ServiceAccount token"
TOKEN=""
for _ in $(seq 1 30); do
  TOKEN=$(kubectl -n "$NS" get secret "${SA}-token" -o jsonpath='{.data.token}' 2>/dev/null || true)
  [ -n "$TOKEN" ] && break
  sleep 2
done
[ -n "$TOKEN" ] || { echo "token never appeared in secret ${SA}-token" >&2; exit 1; }
TOKEN=$(printf '%s' "$TOKEN" | base64 -d)

# The CA that signs the API server's SERVING certificate -- taken from the
# kubeconfig that is demonstrably talking to it right now. The ca.crt inside the
# ServiceAccount secret is a different CA on some distributions, and a mismatch
# surfaces as an x509 error in CI weeks later.
CTX=$(kubectl config current-context)
CLUSTER=$(kubectl config view -o jsonpath="{.contexts[?(@.name==\"$CTX\")].context.cluster}")
CA=$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$CLUSTER\")].cluster.certificate-authority-data}")
if [ -z "$CA" ]; then
  CA_FILE=$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$CLUSTER\")].cluster.certificate-authority}")
  [ -n "$CA_FILE" ] || { echo "cannot find the cluster CA in your kubeconfig" >&2; exit 1; }
  CA=$(base64 < "$CA_FILE" | tr -d '\n')
fi
LOCAL_SERVER=$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$CLUSTER\")].cluster.server}")

TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: recallai
    cluster:
      server: ${KUBE_SERVER}
      certificate-authority-data: ${CA}
users:
  - name: github-deploy
    user:
      token: ${TOKEN}
contexts:
  - name: recallai
    context:
      cluster: recallai
      namespace: ${NS}
      user: github-deploy
current-context: recallai
YAML

# Prove the token works before it is stored, through YOUR tunnel's port rather
# than the runner's. A kubeconfig that only fails in CI is the expensive way to
# find a typo.
echo "==> verifying the token against ${LOCAL_SERVER}"
KUBECONFIG="$TMP" kubectl --server "$LOCAL_SERVER" -n "$NS" auth can-i patch deployments >/dev/null
KUBECONFIG="$TMP" kubectl --server "$LOCAL_SERVER" -n "$NS" auth can-i get secrets 2>/dev/null | grep -qx no \
  && echo "    ok: can patch deployments, cannot read secrets" \
  || echo "    WARNING: this identity can read secrets -- check the Role"

aws ssm put-parameter --region "$REGION" --name "$PARAM" \
  --type SecureString --value "file://$TMP" --overwrite >/dev/null
echo "==> stored at ${PARAM} (SecureString)"
