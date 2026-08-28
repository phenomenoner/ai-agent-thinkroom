"""Container supervisor: loopback Thinkroom plus an internal TCP forwarder."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import signal
import socket
import struct
import subprocess
import sys


async def forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def proxy(
    client: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_port: int,
    gateway: str,
) -> None:
    if not client_allowed(client_writer.get_extra_info("peername"), gateway):
        client_writer.close()
        await client_writer.wait_closed()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", target_port)
        await asyncio.gather(
            forward(client, upstream_writer), forward(upstream_reader, client_writer)
        )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()


def default_gateway_ipv4() -> str:
    try:
        with open("/proc/net/route", encoding="ascii") as routes:
            for line in routes:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))
    except (OSError, UnicodeError, ValueError):
        pass
    raise RuntimeError("container default gateway could not be determined")


def client_allowed(peer: object, gateway: str) -> bool:
    if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
        return False
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError:
        return False
    return address.is_loopback or str(address) == gateway


async def main() -> int:
    app_port = int(os.getenv("THINKROOM_INTERNAL_PORT", "8788"))
    proxy_port = int(os.getenv("THINKROOM_PROXY_PORT", "8787"))
    gateway = default_gateway_ipv4()
    env = {**os.environ, "THINKROOM_HOST": "127.0.0.1", "THINKROOM_PORT": str(app_port)}
    process: subprocess.Popen[bytes] | None = None
    server: asyncio.AbstractServer | None = None
    return_code = 1
    try:
        # Bind the public-facing proxy first, then supervise the loopback app.
        server = await asyncio.start_server(
            lambda r, w: proxy(r, w, app_port, gateway), "0.0.0.0", proxy_port
        )
        process = subprocess.Popen([sys.executable, "-m", "thinkroom", "serve"], env=env)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, process.terminate)
        return_code = await asyncio.to_thread(process.wait)
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return return_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
