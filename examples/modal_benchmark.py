#!/usr/bin/env python3
"""
Modal QUIC Portal Benchmark

This benchmark tests the round-trip latency of QUIC Portal communication
using 600KB PING requests and 5KB PONG responses over 50 iterations.

Usage:
    modal run modal_benchmark.py
"""

import time

import modal

# Create Modal app
app = modal.App("quic-portal-benchmark")

# Modal image with quic-portal installed
image = (
    modal.Image.debian_slim()
    .pip_install("maturin")
    .run_commands("apt-get update && apt-get install -y build-essential pkg-config libssl-dev curl")
    .run_commands(
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y",
        ". $HOME/.cargo/env",
    )
    # Copy and build quic-portal
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

# Benchmark constants
PING_SIZE = 600 * 1024  # 600KB
PONG_SIZE = 5 * 1024  # 5KB
N_ITERATIONS = 50
SERVER_REGION = "us-sanjose-1"
CLIENT_REGION = "us-west-1"


@app.function(image=image, region=SERVER_REGION)
def run_benchmark_server(coord_dict: modal.Dict):
    """Benchmark server that receives PING requests and sends PONG responses."""
    from quic_portal import Portal

    print(f"[SERVER] Starting benchmark server for {N_ITERATIONS} round trips...")
    print(f"[SERVER] PING size: {PING_SIZE/1024:.1f}KB, PONG size: {PONG_SIZE/1024:.1f}KB")

    # Create server with NAT traversal
    portal = Portal.create_server(dict=coord_dict, local_port=5555)

    print("[SERVER] Connected! Waiting for PING requests...")

    # Create 5KB PONG response data once
    pong_data = b"P" * PONG_SIZE

    # Handle PING/PONG exchanges
    rounds_completed = 0
    while rounds_completed < N_ITERATIONS:
        # Receive PING request
        ping_data = portal.recv(timeout_ms=30000)  # 30 second timeout
        if ping_data is None:
            print("[SERVER] Timeout waiting for PING request")
            break

        if len(ping_data) != PING_SIZE:
            print(
                f"[SERVER] Warning: Received PING size {len(ping_data)} bytes, expected {PING_SIZE}"
            )

        rounds_completed += 1
        print(f"[SERVER] Received PING {rounds_completed}/{N_ITERATIONS}: {len(ping_data)} bytes")

        # Send PONG response
        portal.send(pong_data)
        print(f"[SERVER] Sent PONG {rounds_completed}: {len(pong_data)} bytes")

    print(f"[SERVER] Benchmark completed! Handled {rounds_completed} round trips")
    time.sleep(1.0)
    portal.close()

    return {
        "rounds_completed": rounds_completed,
        "ping_size": PING_SIZE,
        "pong_size": PONG_SIZE,
    }


@app.function(image=image, region=CLIENT_REGION)
def run_benchmark_client(coord_dict: modal.Dict):
    """Benchmark client that sends PING requests and measures round-trip latency."""
    from quic_portal import Portal

    print(f"[CLIENT] Starting benchmark client for {N_ITERATIONS} round trips...")
    print(f"[CLIENT] PING size: {PING_SIZE/1024:.1f}KB, PONG size: {PONG_SIZE/1024:.1f}KB")

    # Create client with NAT traversal
    portal = Portal.create_client(dict=coord_dict, local_port=5556)

    print("[CLIENT] Connected! Starting latency benchmark...")

    # Create 600KB PING request data once
    ping_data = b"P" * PING_SIZE

    # Store latency measurements
    latencies = []

    # Send PING requests and measure round-trip latency
    for i in range(N_ITERATIONS):
        print(f"[CLIENT] Starting round trip {i+1}/{N_ITERATIONS}")

        # Start timing
        start_time = time.monotonic()

        # Send PING request
        portal.send(ping_data)
        print(f"[CLIENT] Sent PING {i+1}: {len(ping_data)} bytes")

        # Wait for PONG response
        pong_response = portal.recv(timeout_ms=10000)  # 10 second timeout

        # End timing
        end_time = time.monotonic()

        if pong_response:
            latency = end_time - start_time
            latencies.append(latency * 1000)  # Convert to milliseconds

            print(f"[CLIENT] Received PONG {i+1}: {len(pong_response)} bytes")
            print(f"[CLIENT] Round trip {i+1} latency: {latency * 1000:.2f}ms")

            if len(pong_response) != PONG_SIZE:
                print(
                    f"[CLIENT] Warning: Received PONG size {len(pong_response)} bytes, expected {PONG_SIZE}"
                )
        else:
            print(f"[CLIENT] No PONG response for round trip {i+1}")
            break

        # Small pause between iterations
        time.sleep(0.1)

    portal.close()

    # Calculate statistics
    if latencies:
        latencies.sort()
        num_latencies = len(latencies)

        avg_latency = sum(latencies) / num_latencies
        p50_latency = latencies[num_latencies // 2]
        p75_latency = latencies[int(num_latencies * 0.75)]
        p90_latency = latencies[int(num_latencies * 0.9)]
        min_latency = latencies[0]
        max_latency = latencies[-1]

        return {
            "rounds_completed": len(latencies),
            "ping_size": PING_SIZE,
            "pong_size": PONG_SIZE,
            "latencies": latencies,
            "avg_latency": avg_latency,
            "p50_latency": p50_latency,
            "p75_latency": p75_latency,
            "p90_latency": p90_latency,
            "min_latency": min_latency,
            "max_latency": max_latency,
        }
    else:
        return {
            "rounds_completed": 0,
            "ping_size": PING_SIZE,
            "pong_size": PONG_SIZE,
            "error": "No successful round trips",
        }


@app.local_entrypoint()
def main(local: bool = False):
    """Main benchmark entrypoint."""

    print("🚀 Starting QUIC Portal Round-Trip Latency Benchmark")
    print(f"📈 Iterations: {N_ITERATIONS}")
    print(f"📦 PING size: {PING_SIZE/1024:.1f}KB")
    print(f"📦 PONG size: {PONG_SIZE/1024:.1f}KB")

    # Create ephemeral Modal Dict for coordination
    with modal.Dict.ephemeral() as coord_dict:
        # Start server
        print("📡 Spawning benchmark server...")
        server_task = run_benchmark_server.spawn(coord_dict)

        # Give server time to start
        time.sleep(3)

        # Run client
        print("🔌 Starting benchmark client...")
        try:
            if local:
                client_results = run_benchmark_client.local(coord_dict)
            else:
                client_results = run_benchmark_client.remote(coord_dict)

            # Get server results
            print("📊 Getting server results...")
            server_results = server_task.get()

            # Print summary
            print("\n" + "=" * 60)
            print("📊 QUIC PORTAL ROUND-TRIP LATENCY BENCHMARK RESULTS")
            print("=" * 60)
            print(f"Iterations: {N_ITERATIONS}")
            print(f"PING size: {PING_SIZE/1024:.1f}KB")
            print(f"PONG size: {PONG_SIZE/1024:.1f}KB")
            print(f"Client rounds completed: {client_results['rounds_completed']}")
            print(f"Server rounds completed: {server_results['rounds_completed']}")

            if "avg_latency" in client_results:
                print("\nLatency Statistics:")
                print(f"Average latency: {client_results['avg_latency']:.2f}ms")
                print(f"Median latency (p50): {client_results['p50_latency']:.2f}ms")
                print(f"75th percentile (p75): {client_results['p75_latency']:.2f}ms")
                print(f"90th percentile (p90): {client_results['p90_latency']:.2f}ms")
                print(f"Min latency: {client_results['min_latency']:.2f}ms")
                print(f"Max latency: {client_results['max_latency']:.2f}ms")
            elif "error" in client_results:
                print(f"\n❌ Error: {client_results['error']}")

            print("=" * 60)

        except Exception as e:
            print(f"❌ Benchmark failed: {e}")
            server_task.cancel()

    print("✨ Benchmark completed!")


if __name__ == "__main__":
    print("Use 'modal run modal_benchmark.py' to run this benchmark")
