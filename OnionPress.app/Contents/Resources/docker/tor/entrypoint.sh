#!/bin/sh

# Polling-only mode: minimal Tor instance for cellar healthchecks.
# No hidden services, no healthcheck server — just a SOCKS proxy.
if [ "${POLLING_ONLY}" = "1" ]; then
    mkdir -p /var/lib/tor/data
    cat > /etc/tor/torrc <<EOF
SocksPort 0.0.0.0:9050
DataDirectory /var/lib/tor/data
EOF
    chown -R tor:tor /var/lib/tor
    exec su-exec tor tor
fi

# Fix ownership on mounted volume (may be root-owned from previous goldy image)
chown -R tor:tor /var/lib/tor/hidden_service
chmod 700 /var/lib/tor/hidden_service

# Ensure healthcheck hidden service directory exists with correct permissions
mkdir -p /var/lib/tor/hidden_service/healthcheck
chown -R tor:tor /var/lib/tor/hidden_service/healthcheck
chmod 700 /var/lib/tor/hidden_service/healthcheck

# Write version for healthcheck server
echo "${ONIONPRESS_VERSION:-unknown}" > /var/lib/tor/healthcheck-version

# Start healthcheck HTTP server in background (port 8081)
/healthcheck-server.sh &

# Start cellar redirect service in background (port 8082) when in cellar mode
if [ "${ONIONPRESS_CELLAR}" = "1" ]; then
    echo "OnionCellar mode: starting redirect service..."
    mkdir -p /var/lib/tor/hidden_service/cellar
    chown -R tor:tor /var/lib/tor/hidden_service/cellar
    /cellar-redirect.sh &
fi

# Drop privileges and run tor
exec su-exec tor tor
