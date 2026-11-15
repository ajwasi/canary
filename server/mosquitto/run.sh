#!/bin/sh
# Starts the broker with the server certificate that Caddy obtained and the
# device CA that the app created. Re-copies them twice a day and tells the
# broker to reload, so certificate renewals are picked up without downtime.
SRC="/caddy/caddy/certificates/acme-v02.api.letsencrypt.org-directory/$DOMAIN"
mkdir -p /certs

copy_certs() {
    cp "$SRC/$DOMAIN.crt" /certs/server.crt &&
    cp "$SRC/$DOMAIN.key" /certs/server.key &&
    cp /appdata/ca/device-ca.pem /certs/device-ca.pem &&
    chown mosquitto:mosquitto /certs/* && chmod 600 /certs/server.key
}

until copy_certs 2>/dev/null; do
    echo "waiting for the server certificate and device CA..."
    sleep 5
done

(while sleep 43200; do copy_certs && kill -HUP 1; done) &
exec mosquitto -c /mosquitto/config/mosquitto.conf
