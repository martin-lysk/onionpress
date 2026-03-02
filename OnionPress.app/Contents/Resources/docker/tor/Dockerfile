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
COPY arti-cellar.toml /etc/arti/arti-cellar.toml
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck-server.sh /healthcheck-server.sh
COPY cellar-redirect.sh /cellar-redirect.sh
COPY cellar-tor-manager.sh /cellar-tor-manager.sh
COPY cellar-poller.py /cellar-poller.py
COPY cellar-server.py /cellar-server.py
RUN chmod +x /entrypoint.sh /healthcheck-server.sh /cellar-redirect.sh /cellar-tor-manager.sh /cellar-poller.py /cellar-server.py
ENTRYPOINT ["/entrypoint.sh"]
