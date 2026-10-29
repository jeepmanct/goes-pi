#!/bin/bash
# certbot deploy hook: copy the renewed cert where the web UI reads it, then restart the UI.
set -e
DOMAIN=${DOMAIN:-your-domain.example.com}   # the name certbot issued the cert for
DEST=/home/pi/goes/webui/tls
mkdir -p "$DEST"
cp "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "$DEST/fullchain.pem"
cp "/etc/letsencrypt/live/$DOMAIN/privkey.pem" "$DEST/privkey.pem"
chown -R pi:pi "$DEST"
chmod 600 "$DEST/privkey.pem"
systemctl restart goes-webui 2>/dev/null || true
