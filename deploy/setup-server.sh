#!/usr/bin/env bash
# Sets up a fresh Ubuntu VM (Oracle Cloud Always Free) to host the chatbot; see deploy/README.md.
# Run it on the VM as the default user. It's safe to run again: it skips what's done, and once
# .env is filled in it builds and starts (or updates) the app.
set -euo pipefail

REPO=${REPO:-https://github.com/CYENS/smart-nicosia-api-exploration.git}
BRANCH=${BRANCH:-main}
DIR=${DIR:-$HOME/smart-nicosia}

# Oracle's Ubuntu images only let SSH through iptables. Open HTTP and HTTPS (and HTTP/3), and keep
# the rules across reboots. This runs before Docker is installed, so Docker's rules aren't saved.
added=0
for rule in "tcp 80" "tcp 443" "udp 443"; do
  set -- $rule
  if ! sudo iptables -C INPUT -p "$1" --dport "$2" -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 1 -p "$1" --dport "$2" -j ACCEPT
    added=1
  fi
done
if [ "$added" = 1 ] && command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save
fi

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

if [ ! -d "$DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"
git pull --ff-only

# Secrets stay on the VM, readable only by this user. Without a domain of your own, the
# VM's public IP under sslip.io gets a real certificate too.
if [ ! -f .env ]; then
  ip=$(curl -fsS https://api.ipify.org || true)
  (umask 077 && printf 'ANTHROPIC_API_KEY=\nCHATBOT_PASSWORD=\nDOMAIN=%s\n' "${ip:+$ip.sslip.io}" > .env)
fi
if grep -qE '^(ANTHROPIC_API_KEY|CHATBOT_PASSWORD|DOMAIN)=[[:space:]]*$' .env; then
  echo
  echo "Next: fill in ANTHROPIC_API_KEY, CHATBOT_PASSWORD and DOMAIN in $DIR/.env"
  echo "  nano $DIR/.env"
  echo "then run this script again."
  exit 0
fi

sudo docker compose up -d --build </dev/null
echo
echo "Up at https://$(sed -n 's/^DOMAIN=//p' .env) (the certificate can take a minute)."
echo "Logs: cd $DIR && sudo docker compose logs -f chatbot"
