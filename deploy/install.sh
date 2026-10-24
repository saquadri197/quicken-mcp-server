#!/usr/bin/env bash
# Install quicken-mcp-server in a Debian 12 LXC (or VM) and lock it down so the
# Claude desktop app on another machine can reach it only over SSH.
#
# Run as root inside the container:
#   bash install.sh <git-repo-url> "<ssh public key from your Windows PC>"
#
# What it sets up:
#   - user "quicken" with no password (SSH key login only)
#   - code in /opt/quicken-mcp, Python venv in /opt/quicken-mcp/venv
#   - export drop folder /srv/quicken-data (put your Quicken CSV exports here)
#   - the SSH key is restricted to running the MCP server: no shell, no
#     port forwarding, no PTY. Even if the key leaked, it could only start the
#     read-only server.
set -euo pipefail

REPO_URL="${1:?usage: install.sh <git-repo-url> \"<ssh-public-key>\"}"
PUBKEY="${2:?usage: install.sh <git-repo-url> \"<ssh-public-key>\"}"
APP_DIR=/opt/quicken-mcp
DATA_DIR=/srv/quicken-data
SVC_USER=quicken

echo "==> Installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv git openssh-server >/dev/null

echo "==> Creating user $SVC_USER"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$SVC_USER"
passwd -l "$SVC_USER" >/dev/null

echo "==> Fetching code"
if [ -d "$APP_DIR/src/.git" ]; then
  git -C "$APP_DIR/src" pull -q
else
  mkdir -p "$APP_DIR"
  git clone -q "$REPO_URL" "$APP_DIR/src"
fi

echo "==> Building Python environment"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q "mcp>=1.2,<2" "duckdb>=0.9.0,<2"

echo "==> Creating data folder $DATA_DIR"
mkdir -p "$DATA_DIR"
chown root:"$SVC_USER" "$DATA_DIR"
chmod 2770 "$DATA_DIR"

echo "==> Writing launcher"
cat > "$APP_DIR/run.sh" <<EOF
#!/usr/bin/env bash
# Started by SSH for each Claude session; speaks MCP over stdin/stdout.
cd "$APP_DIR/src"
exec "$APP_DIR/venv/bin/python" -m app.main --csv "$DATA_DIR" --server-mode stdio --log-level WARNING --memory-limit 512MB
EOF
chmod 755 "$APP_DIR/run.sh"
chown -R root:root "$APP_DIR"

echo "==> Installing restricted SSH key"
SSH_DIR="/home/$SVC_USER/.ssh"
mkdir -p "$SSH_DIR"
echo "command=\"$APP_DIR/run.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $PUBKEY" > "$SSH_DIR/authorized_keys"
chown -R "$SVC_USER":"$SVC_USER" "$SSH_DIR"
chmod 700 "$SSH_DIR"; chmod 600 "$SSH_DIR/authorized_keys"

systemctl enable --now ssh >/dev/null 2>&1 || true

# Optional: a Windows network share for dropping exports into the data folder.
#   SAMBA_USER=syed bash install.sh ...   (you'll be asked for a share password)
if [ -n "${SAMBA_USER:-}" ]; then
  echo "==> Setting up Samba share \\\\$(hostname)\\quicken-data for $SAMBA_USER"
  apt-get install -y -qq samba >/dev/null
  id "$SAMBA_USER" >/dev/null 2>&1 || useradd --no-create-home --shell /usr/sbin/nologin "$SAMBA_USER"
  usermod -aG "$SVC_USER" "$SAMBA_USER"
  if ! grep -q '^\[quicken-data\]' /etc/samba/smb.conf; then
    cat >> /etc/samba/smb.conf <<EOF

[quicken-data]
   path = $DATA_DIR
   valid users = $SAMBA_USER
   read only = no
   create mask = 0660
   directory mask = 2770
   force group = $SVC_USER
EOF
  fi
  smbpasswd -a "$SAMBA_USER"
  systemctl restart smbd
fi

IP=$(hostname -I | awk '{print $1}')
echo
echo "Done. Next:"
if [ -n "${SAMBA_USER:-}" ]; then
  echo "  1. Save Quicken CSV exports to \\\\$IP\\quicken-data (the newest .csv is used)."
else
  echo "  1. Copy a Quicken CSV export into $DATA_DIR (the newest .csv is used),"
  echo "     e.g. from the Proxmox host: pct push <CTID> export.csv $DATA_DIR/export.csv"
fi
echo "  2. In the Claude desktop app config on Windows, connect with: ssh $SVC_USER@$IP"
