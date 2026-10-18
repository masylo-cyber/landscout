#!/usr/bin/env bash
# LandScout – instalator na Ubuntu 22.04/24.04. Uruchamiaj jako root:
#   curl -fsSL https://raw.githubusercontent.com/masylo-cyber/landscout/main/install.sh | bash
set -euo pipefail

REPO="https://github.com/masylo-cyber/landscout.git"
DIR="/opt/landscout"

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
ask() { # ask VAR "pytanie" [domyślna]
  local var="$1" q="$2" def="${3:-}" val
  if [ -n "$def" ]; then read -r -p "$q [$def]: " val </dev/tty; val="${val:-$def}"
  else read -r -p "$q: " val </dev/tty; fi
  printf -v "$var" '%s' "$val"
}
ask_secret() {
  local var="$1" q="$2" val
  read -r -s -p "$q: " val </dev/tty; echo
  printf -v "$var" '%s' "$val"
}

[ "$(id -u)" -eq 0 ] || { echo "Uruchom jako root."; exit 1; }

say "Pakiety systemowe"
apt-get update -qq
apt-get install -y -qq curl git ufw fail2ban >/dev/null

if ! command -v docker >/dev/null; then
  say "Instaluję Docker"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null

say "Firewall"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null

say "Kod aplikacji -> $DIR"
git config --global --add safe.directory "$DIR" >/dev/null 2>&1 || true
if [ -d "$DIR" ] && [ ! -d "$DIR/.git" ]; then
  # katalog założony wcześniej (np. ręcznie) – przejmujemy go i czyścimy
  chown -R root:root "$DIR"
  rm -rf "$DIR"
fi
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

if [ ! -f .env ]; then
  say "Konfiguracja (klucze zostają tylko na tym serwerze, w $DIR/.env)"
  DB_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
  ask_secret ANTHROPIC_API_KEY "Klucz API Anthropic"
  ask_secret APIFY_TOKEN "Token Apify (Enter, jeśli brak)"
  ask_secret TELEGRAM_BOT_TOKEN "Token bota Telegram (od @BotFather)"
  ask SMTP_HOST "Serwer SMTP (Enter, by pominąć mail)" ""
  SMTP_PORT=587; SMTP_USER=""; SMTP_PASSWORD=""; MAIL_FROM=""; MAIL_TO=""
  if [ -n "$SMTP_HOST" ]; then
    ask SMTP_PORT "Port SMTP" "587"
    ask SMTP_USER "Login SMTP" ""
    ask_secret SMTP_PASSWORD "Hasło SMTP"
    ask MAIL_FROM "Adres nadawcy" "$SMTP_USER"
    ask MAIL_TO "Adres(y) odbiorcy, po przecinku" "$SMTP_USER"
  fi
  ask PANEL_USER "Login do panelu WWW" "admin"
  PANEL_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)"

  cat > .env <<EOF
DB_PASSWORD=$DB_PASSWORD
ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
CLAUDE_MODEL=claude-sonnet-5-5
APIFY_TOKEN=$APIFY_TOKEN
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID_GALANO=
TELEGRAM_CHAT_ID_BAURO=
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_USER=$SMTP_USER
SMTP_PASSWORD=$SMTP_PASSWORD
MAIL_FROM=$MAIL_FROM
MAIL_TO=$MAIL_TO
PANEL_USER=$PANEL_USER
PANEL_PASSWORD=$PANEL_PASSWORD
RUN_INTERVAL_MIN=120
MAX_LLM_PER_RUN=60
EOF
  chmod 600 .env
  echo
  echo "Hasło do panelu WWW: $PANEL_PASSWORD   (zapisane w $DIR/.env)"
fi

say "Komenda 'landscout'"
install -m 755 bin/landscout /usr/local/bin/landscout

say "Budowa i start kontenerów (pierwszy raz 2–5 min)"
docker compose up -d --build

say "Gotowe"
cat <<EOF

Status:        landscout status
Logi:          landscout logs
Przebieg ręczny: landscout run
Aktualizacja:  landscout update
Panel WWW:     landscout panel   (tunel SSH / instrukcja)

Następny krok: napisz do swojego bota na Telegramie "/start galano" i "/start bauro"
(z czatów, na które mają przychodzić alerty), potem: landscout telegram-setup
EOF
