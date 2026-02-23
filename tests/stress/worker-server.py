#!/usr/bin/env python3
"""
Multi-port HTTP server for cellar stress testing.

Replaces hundreds of individual socat processes with a single process.
Listens on a range of ports (2 per worker: content + healthcheck) and
serves simple HTTP responses. A control API on port 9000 allows the
stress test script to toggle individual ports on/off to simulate failures.

Usage:
    python3 worker-server.py <base_port> <num_workers>
    python3 worker-server.py 9100 50

Each worker i gets:
    content port:     base_port + i*2
    healthcheck port: base_port + i*2 + 1
"""

import asyncio
import json
import sys

# Set of disabled ports (simulating failure)
disabled_ports = set()

# Stats
stats = {"requests": 0, "disabled_hits": 0, "healthy_hits": 0}


async def handle_http(reader, writer, port):
    """Handle an HTTP request. Close without response if port is disabled."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not request_line:
            writer.close()
            return

        # Consume headers
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break

        stats["requests"] += 1

        if port in disabled_ports:
            # Simulate failure: close without response → curl exit code 52
            stats["disabled_hits"] += 1
            writer.close()
            return

        stats["healthy_hits"] += 1
        body = b"<html><body>OK</body></html>"
        response = (
            f"HTTP/1.0 200 OK\r\n"
            f"Content-Type: text/html\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_control(reader, writer):
    """Control API on port 9000."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not request_line:
            writer.close()
            return

        parts = request_line.decode(errors="replace").split()
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key, val = line.decode(errors="replace").split(":", 1)
                headers[key.strip().lower()] = val.strip()

        body = b""
        cl = int(headers.get("content-length", 0))
        if cl > 0:
            body = await asyncio.wait_for(reader.readexactly(cl), timeout=10)

        if path == "/disable" and method == "POST":
            data = json.loads(body)
            ports = data.get("ports", [])
            disabled_ports.update(ports)
            resp = json.dumps({"ok": True, "disabled": sorted(disabled_ports)}).encode()
        elif path == "/enable" and method == "POST":
            data = json.loads(body)
            ports = data.get("ports", [])
            disabled_ports.difference_update(ports)
            resp = json.dumps({"ok": True, "disabled": sorted(disabled_ports)}).encode()
        elif path == "/status":
            resp = json.dumps({
                "disabled_count": len(disabled_ports),
                "stats": stats,
            }).encode()
        else:
            writer.write(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        writer.write(
            f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp)}\r\n\r\n".encode()
            + resp
        )
        await writer.drain()
    except Exception as e:
        print(f"Control error: {e}", flush=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    base_port = int(sys.argv[1]) if len(sys.argv) > 1 else 9100
    num_workers = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    servers = []
    total_ports = num_workers * 2

    # Start a listener on each worker port
    for i in range(total_ports):
        port = base_port + i
        srv = await asyncio.start_server(
            lambda r, w, p=port: handle_http(r, w, p),
            "127.0.0.1", port,
        )
        servers.append(srv)

    # Control API
    control = await asyncio.start_server(handle_control, "0.0.0.0", 9000)
    servers.append(control)

    print(
        f"Worker server: {total_ports} ports ({base_port}-{base_port + total_ports - 1}) "
        f"+ control on 9000",
        flush=True,
    )

    await asyncio.gather(*(srv.serve_forever() for srv in servers))


if __name__ == "__main__":
    asyncio.run(main())
