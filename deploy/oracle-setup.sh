#!/bin/bash
# Free hosting: an Oracle Cloud "Always Free" Ubuntu VM (Ampere ARM, up to 4 cores / 24 GB). Run on the VM:
#   curl -fsSL https://raw.githubusercontent.com/DivakarUpadhyay/jobBot/main/deploy/oracle-setup.sh | bash
# Optional first: copy your data next to it as ~/seed.tar.gz (made on the PC with tools/push_seed.py --file).
# Installs Docker, fetches the code, writes deploy/.env with free HTTPS addresses on sslip.io, loads the data
# into the volume when ~/seed.tar.gz is there, and starts the service. Running it again updates the code.
set -euo pipefail
REPO=https://github.com/DivakarUpadhyay/jobBot.git
DIR=$HOME/jobBot

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
DOCKER="sudo docker"

# Ubuntu images on Oracle block everything but SSH in iptables; open 80 and 443 for the HTTPS proxy.
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport $port -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 5 -p tcp --dport $port -j ACCEPT
done
sudo apt-get install -y -q iptables-persistent >/dev/null 2>&1 || true
sudo netfilter-persistent save >/dev/null 2>&1 || true

# A swap file so Chrome never runs the small VMs out of memory.
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone "$REPO" "$DIR"; fi
cd "$DIR"

if [ ! -f deploy/.env ]; then
  IP=$(curl -fsS https://api.ipify.org)
  HOST="${IP//./-}.sslip.io"
  sed -e "s|^DOMAIN=.*|DOMAIN=$HOST|" \
      -e "s|^ADMIN_DOMAIN=.*|ADMIN_DOMAIN=admin.$HOST|" \
      -e "s|^JOBBOT_PUBLIC_URL=.*|JOBBOT_PUBLIC_URL=https://$HOST|" deploy/env.example > deploy/.env
  if [ "$(dpkg --print-architecture)" != "amd64" ]; then echo "JOBBOT_BROWSER_CHANNEL=chromium" >> deploy/.env; fi
  echo "Wrote deploy/.env (addresses https://$HOST and https://admin.$HOST). Add GEMINI_API_KEY there."
fi

COMPOSE="$DOCKER compose -f deploy/docker-compose.yml --env-file deploy/.env"
$COMPOSE build
if [ -f "$HOME/seed.tar.gz" ]; then
  $COMPOSE run --rm --no-deps -v "$HOME/seed.tar.gz:/seed.tar.gz:ro" --entrypoint sh admin \
    -c 'if [ -f /data/jobbot.db ]; then echo "data already there, seed not loaded"; else tar xzf /seed.tar.gz -C /data && echo "data loaded"; fi'
  mv "$HOME/seed.tar.gz" "$HOME/seed.loaded.tar.gz"
fi
$COMPOSE up -d
$COMPOSE ps
grep -E '^(DOMAIN|ADMIN_DOMAIN)=' deploy/.env
