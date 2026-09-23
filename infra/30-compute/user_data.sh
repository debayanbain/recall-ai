#!/bin/bash
set -euxo pipefail
# -e stop on first error | -u stop on unset variable
# -x print each command   | pipefail: a failing pipe fails the script
exec > >(tee /var/log/user-data.log) 2>&1

echo "=== user-data start ==="

# ── 1. SSM Agent: your ONLY way into this machine ──────────
# Ubuntu AMIs ship it as a snap. Make sure it runs.
snap list amazon-ssm-agent || snap install amazon-ssm-agent --classic
snap start amazon-ssm-agent || true

# ── 2. Swap: a safety net for 4 GB RAM ─────────────────────
# Without swap, when RAM runs out, Linux kills a random process.
# With swap, the server gets slow instead.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=10
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf

# ── 3. Basic tools ──────────────────────────────────────────
apt-get update -y
apt-get install -y curl unzip jq

# ── 4. AWS CLI (ARM build) — for debugging from the node ───
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp
/tmp/aws/install --update
rm -rf /tmp/aws /tmp/awscli.zip

# ── 5. K3s ──────────────────────────────────────────────────
# ServiceLB is KEPT. It binds ports 80/443 on the host and sends
# them to Traefik. Disable it and nothing from the internet arrives.
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --write-kubeconfig-mode 644 \
  --node-name k3s-node" sh -

# Wait until Kubernetes is really ready
until k3s kubectl get nodes 2>/dev/null | grep -q " Ready "; do
  echo "waiting for k3s..."
  sleep 5
done

# ── 6. kubectl for the ubuntu user ─────────────────────────
mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
chmod 600 /home/ubuntu/.kube/config
echo 'export KUBECONFIG=/home/ubuntu/.kube/config' >> /home/ubuntu/.bashrc


# ── ECR pull credentials for K3s ────────────────────────────
# ECR login tokens expire after 12 hours. This timer refreshes the
# Kubernetes pull secret every 6 hours using the node's IAM role,
# so no registry password is ever stored on disk.
cat > /usr/local/bin/ecr-refresh.sh <<'SCRIPT'
#!/bin/bash
set -euo pipefail
REGION=ap-south-1
REGISTRY="179793764711.dkr.ecr.$REGION.amazonaws.com"
NS=recallai

k3s kubectl get namespace $NS >/dev/null 2>&1 || k3s kubectl create namespace $NS

TOKEN=$(aws ecr get-login-password --region $REGION)

k3s kubectl create secret docker-registry ecr-pull -n $NS \
  --docker-server="$REGISTRY" --docker-username=AWS --docker-password="$TOKEN" \
  --dry-run=client -o yaml | k3s kubectl apply -f -

echo "$(date -Is) ECR pull secret refreshed"
SCRIPT
chmod +x /usr/local/bin/ecr-refresh.sh

cat > /etc/systemd/system/ecr-refresh.service <<'UNIT'
[Unit]
Description=Refresh ECR pull secret for K3s
After=k3s.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/ecr-refresh.sh
UNIT

cat > /etc/systemd/system/ecr-refresh.timer <<'UNIT'
[Unit]
Description=Run ecr-refresh every 6 hours
[Timer]
OnBootSec=2min
OnUnitActiveSec=6h
Persistent=true
[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now ecr-refresh.timer
systemctl start ecr-refresh.service

echo "=== user-data finished successfully ==="
