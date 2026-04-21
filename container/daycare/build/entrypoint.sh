#!/bin/bash
set -e

# ── Copy secrets for daycare user ─────────────────────────────────────────────
SECRETS_SRC="/run/secrets"
SECRETS_DST="/run/daycare-secrets"

mkdir -p "$SECRETS_DST"

for secret in radicale_password smtp_password; do
    src="$SECRETS_SRC/$secret"
    dst="$SECRETS_DST/$secret"
    if [ -f "$src" ]; then
        cp "$src" "$dst"
        chown daycare:daycare "$dst"
        chmod 400 "$dst"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S'),entrypoint,WARN,,daycare,secret_missing,$secret"
    fi
done

chmod 500 "$SECRETS_DST"
chown daycare:daycare "$SECRETS_DST"

# ── Make /data writable for daycare ───────────────────────────────────────────
chown -R daycare:daycare /data 2>/dev/null || true

# ── Start supervisord (programs run as daycare via supervisord.conf) ──────────
echo "$(date '+%Y-%m-%d %H:%M:%S'),entrypoint,INFO,,daycare,start,supervisord uid=1500"
exec supervisord -n -c /etc/supervisor/conf.d/daycare.conf
