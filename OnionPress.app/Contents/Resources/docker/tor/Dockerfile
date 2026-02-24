# Stage 1: Compile Arti from source
FROM rust:latest AS builder
RUN cargo install arti --locked --features onion-service-service

# Stage 2: Runtime (must match builder's glibc — rust:latest uses trixie)
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget socat openssl ca-certificates libsqlite3-0 sqlite3 python3 curl && rm -rf /var/lib/apt/lists/*
RUN useradd -r -m -s /bin/false arti
COPY --from=builder /usr/local/cargo/bin/arti /usr/local/bin/arti
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
