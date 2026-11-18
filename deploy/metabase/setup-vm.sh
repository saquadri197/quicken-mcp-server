#!/usr/bin/env bash
# One-time setup of the Metabase VM (Ubuntu Server 24.04 LTS).
# Run as root on the new VM:
#   SHARE_USER=<samba user> bash setup-vm.sh <git-repo-url>
# You'll be asked for the share password; it is stored in /root/.smb-quicken (mode 600).
set -euo pipefail

REPO_URL="${1:?usage: SHARE_USER=<samba user> bash setup-vm.sh <git-repo-url>}"
SHARE="//192.168.3.136/quicken-data"
MOUNT=/mnt/quicken-data
APP=/opt/quicken-mcp-server
: "${SHARE_USER:?set SHARE_USER to the Samba user for the quicken-data share}"

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 cifs-utils git open-vm-tools >/dev/null
systemctl enable --now docker

echo "==> Read-only mount of the quicken-data share"
if [ ! -f /root/.smb-quicken ]; then
  read -rsp "Password for $SHARE_USER on $SHARE: " SHARE_PASS; echo
  printf 'username=%s\npassword=%s\n' "$SHARE_USER" "$SHARE_PASS" > /root/.smb-quicken
  chmod 600 /root/.smb-quicken
fi
mkdir -p "$MOUNT"
grep -q " $MOUNT " /etc/fstab || echo "$SHARE $MOUNT cifs ro,credentials=/root/.smb-quicken,vers=3.0,uid=0,gid=0,file_mode=0444,dir_mode=0555,_netdev,x-systemd.automount 0 0" >> /etc/fstab
systemctl daemon-reload
mount "$MOUNT" 2>/dev/null || true
ls "$MOUNT" | tail -3

echo "==> Code"
if [ -d "$APP/.git" ]; then git -C "$APP" pull -q; else git clone -q "$REPO_URL" "$APP"; fi

cd "$APP/deploy/metabase"
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^PG_ADMIN_PASSWORD=.*/PG_ADMIN_PASSWORD=$(openssl rand -hex 24)/" .env
  sed -i "s/^PG_READER_PASSWORD=.*/PG_READER_PASSWORD=$(openssl rand -hex 24)/" .env
  chmod 600 .env
  echo "Created .env with random database passwords. Check METABASE_VERSION and VM_HOSTNAME in it."
fi

echo "==> Start"
docker compose up -d
echo
echo "Done. In a minute or two, open http://$(hostname -I | awk '{print $1}'):3000 and create your Metabase admin login."
echo "Add the finance data as a PostgreSQL database: host postgres, port 5432, database finance,"
echo "user finance_reader, password = PG_READER_PASSWORD from $APP/deploy/metabase/.env"
