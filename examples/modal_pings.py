"""
Simple Modal QUIC Portal Example

This example demonstrates basic bidirectional communication using Portal static methods:
1. Server and client coordinate via ephemeral Modal Dict
2. NAT traversal handled automatically by Portal.create_server/create_client
3. Simple message exchange over QUIC

Usage:
    modal run modal_simple.py
"""

import random
import modal
import time

# Create Modal app
app = modal.App("quic-portal-simple")

# Modal image with quic-portal installed
image = (
    modal.Image.debian_slim()
    .pip_install("maturin")
    .run_commands("apt-get update && apt-get install -y build-essential pkg-config libssl-dev curl")
    .run_commands(
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y",
        ". $HOME/.cargo/env",
    )
    # Copy and build quic-portal (copy=True allows subsequent build steps)
    .add_local_file("pyproject.toml", "/tmp/quic-portal/pyproject.toml", copy=True)
    .add_local_file("Cargo.toml", "/tmp/quic-portal/Cargo.toml", copy=True)
    .add_local_file("README.md", "/tmp/quic-portal/README.md", copy=True)
    .add_local_dir("src", "/tmp/quic-portal/src", copy=True)
    .add_local_dir("python", "/tmp/quic-portal/python", ignore=["__pycache__"], copy=True)
    .run_commands(
        "cd /tmp/quic-portal && . $HOME/.cargo/env && maturin build --release",
        "cd /tmp/quic-portal && pip install target/wheels/*.whl",
    )
)


@app.function(image=image, region="us-sanjose-1")
def run_server(rendezvous: modal.Dict):
    from quic_portal import Portal

    random_port = random.randint(10000, 65535)
    portal = Portal.create_server(rendezvous, local_port=random_port)

    while True:
        msg = portal.recv()
        print(f"[server] Received message: {len(msg)} bytes")
        portal.send(b"pong")
        print("[server] Sent pong")


@app.function(image=image, region="us-west-1")
def run_client():
    from quic_portal import Portal

    with modal.Dict.ephemeral() as rendezvous:
        handle = run_server.spawn(rendezvous)

        random_port = random.randint(10000, 65535)
        portal = Portal.create_client(rendezvous, local_port=random_port)
        print(f"[client] Connected to server, local port: {random_port}")

    def send_ping():
        print("[client] Sending ping ...")
        portal.send(b"ping")
        msg = portal.recv()
        print(f"[client] Received pong: {len(msg)} bytes")

    t = 1.0
    for _ in range(10):
        send_ping()
        print(f"[client] Sleeping for {t} seconds ...")
        time.sleep(t)
        t *= 2

    handle.cancel()


@app.local_entrypoint()
def main():
    run_client.local()


if __name__ == "__main__":
    print("Use 'modal run modal_pings.py' to run this example")
