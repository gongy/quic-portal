#!/usr/bin/env python3
"""
Simple Modal QUIC Portal Example

This example demonstrates basic bidirectional communication using Portal static methods:
1. Server and client coordinate via ephemeral Modal Dict
2. NAT traversal handled automatically by Portal.create_server/create_client
3. Simple message exchange over QUIC

Usage:
    modal run modal_simple.py
"""

import time

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


@app.function(image=image)
def run_server(coord_dict: modal.Dict):
    """Simple server that echoes messages back to client."""
    from quic_portal import Portal

    print("[SERVER] Starting server...")
    Portal.create_server(dict=coord_dict, local_port=5555)
    print("[SERVER] Connected! Waiting for messages...")

    time.sleep(10)


@app.function(image=image)
def run_client(coord_dict: modal.Dict):
    """Simple client that sends messages and receives echoes."""
    from quic_portal import Portal

    print("[CLIENT] Starting client...")
    Portal.create_client(dict=coord_dict, local_port=5556)
    print("[CLIENT] Connected! Sending messages...")

    time.sleep(10)


@app.local_entrypoint()
def main():
    """Main entrypoint that runs server and client."""
    print("🚀 Starting simple QUIC Portal example")

    # Create ephemeral Modal Dict for coordination
    with modal.Dict.ephemeral() as coord_dict:
        # Start server
        print("📡 Spawning server...")
        server_task = run_server.spawn(coord_dict)

        # Give server time to start
        time.sleep(2)

        # Run client
        print("🔌 Starting client...")
        try:
            run_client.remote(coord_dict)
        except Exception as e:
            print(f"❌ Client failed: {e}")

        # Cancel server
        print("🛑 Stopping server...")
        server_task.cancel()

    print("✨ Example completed!")


if __name__ == "__main__":
    print("Use 'modal run modal_simple.py' to run this example")
