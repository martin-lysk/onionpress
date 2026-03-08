# Stage 1: Compile Arti from source — builds native arch (arm64 on Apple Silicon)
# The official Tor Project image is amd64-only, which forces qemu emulation on
# ARM Macs. Building from source gives us a native binary instead.
FROM rust:latest AS builder
RUN cargo install arti --locked --features onion-service-service,static-sqlite

# Stage 2: Runtime (must match builder's glibc — rust:latest uses trixie)
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    socat openssl ca-certificates sqlite3 python3 curl wget procps \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -r -m -s /bin/false arti
COPY --from=builder /usr/local/cargo/bin/arti /usr/local/bin/arti

COPY arti.toml /etc/arti/arti.toml
COPY arti-polling.toml /etc/arti/arti-polling.toml
COPY arti-onionheaven.toml /etc/arti/arti-onionheaven.toml
COPY entrypoint.sh /entrypoint.sh
COPY healthcheck-server.sh /healthcheck-server.sh
COPY onionheaven-redirect.sh /onionheaven-redirect.sh
COPY onionheaven-tor-manager.sh /onionheaven-tor-manager.sh
COPY onion_auth.py /onion_auth.py
COPY onionheaven_common.py /onionheaven_common.py
COPY onionheaven-heartbeat.py /onionheaven-heartbeat.py
COPY onionheaven-server.py /onionheaven-server.py
COPY onionheaven-takeover-worker.py /onionheaven-takeover-worker.py
RUN chmod +x /entrypoint.sh /healthcheck-server.sh /onionheaven-redirect.sh /onionheaven-tor-manager.sh /onionheaven-heartbeat.py /onionheaven-server.py /onionheaven-takeover-worker.py
ENTRYPOINT ["/entrypoint.sh"]
