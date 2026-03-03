# Official Arti image from Tor Project — rebuilt daily, includes onion-service-service
# Alpine-based, arti binary at /usr/local/bin/arti, runs as user arti (UID 1000)
FROM containers.torproject.org/tpo/onion-services/onimages/arti:alpine

# Switch to root for package install and entrypoint (entrypoint drops to arti user)
USER root

# Layer runtime dependencies on top of the official image
RUN apk add --no-cache \
    socat openssl ca-certificates sqlite python3 curl wget procps

COPY arti.toml /etc/arti/arti.toml
COPY arti-polling.toml /etc/arti/arti-polling.toml
COPY arti-onionheaven.toml /etc/arti/arti-onionheaven.toml
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck-server.sh /healthcheck-server.sh
COPY onionheaven-redirect.sh /onionheaven-redirect.sh
COPY onionheaven-tor-manager.sh /onionheaven-tor-manager.sh
COPY onionheaven-poller.py /onionheaven-poller.py
COPY onionheaven-server.py /onionheaven-server.py
RUN chmod +x /entrypoint.sh /healthcheck-server.sh /onionheaven-redirect.sh /onionheaven-tor-manager.sh /onionheaven-poller.py /onionheaven-server.py
ENTRYPOINT ["/entrypoint.sh"]
