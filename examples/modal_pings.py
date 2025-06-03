"""
Simple Modal QUIC Portal Example

This example demonstrates basic bidirectional communication using Portal static methods:
1. Server and client coordinate via ephemeral Modal Dict
2. NAT traversal handled automatically by Portal.create_server/create_client
3. Simple message exchange over QUIC

Usage:
    modal run modal_simple.py
"""

import modal

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
    .add_local_dir("python", "/tmp/quic-portal/python", copy=True)
    .run_commands(
        "cd /tmp/quic-portal && . $HOME/.cargo/env && maturin build --release",
        "cd /tmp/quic-portal && pip install target/wheels/*.whl",
    )
)


@app.function(image=image, region="us-sanjose-1")
def run_server(rendezvous: modal.Dict):
    from quic_portal import Portal

    portal = Portal.create_server(rendezvous)

    # Server sends the first message.
    print("[server] Sending hello ...")
    portal.send(b"hello")


@app.function(image=image, region="us-west-1")
def run_client():
    from quic_portal import Portal

    with modal.Dict.ephemeral() as rendezvous:
        run_server.spawn(rendezvous)
        portal = Portal.create_client(rendezvous)

    msg = portal.recv()
    print(f"[client] Received message: {len(msg)} bytes")


@app.local_entrypoint()
def main():
    run_client.local()


if __name__ == "__main__":
    print("Use 'modal run modal_pings.py' to run this example")
