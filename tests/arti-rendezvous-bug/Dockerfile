# Minimal Arti image built from source (multi-arch: x86_64 + ARM64)
# Usage: docker build -t arti-test .
# Note: Arti 2.1.0 requires Rust >= 1.89. Adjust the base image if needed.
FROM rust:1.89-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev libsqlite3-dev && rm -rf /var/lib/apt/lists/*

# Build Arti 2.1.0 from crates.io
RUN cargo install arti --version 2.1.0 \
    --features onion-service-service,static-sqlite \
    --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 && rm -rf /var/lib/apt/lists/*
RUN useradd -r -m -s /bin/false arti && chmod 700 /home/arti

COPY --from=builder /usr/local/cargo/bin/arti /usr/local/bin/arti

CMD ["sleep", "infinity"]
