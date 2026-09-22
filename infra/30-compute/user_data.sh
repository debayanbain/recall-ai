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

echo "=== user-data finished successfully ==="