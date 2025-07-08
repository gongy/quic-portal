# QUIC Portal (experimental)

> ⚠️ **Warning**: This library is experimental and not intended for production use.

High-performance QUIC communication library with automatic NAT traversal within Modal applications.

## Current features

- **Automatic NAT traversal**: Built-in STUN discovery and UDP hole punching, using Modal Dict for rendezvous.
- **High-performance QUIC**: Rust-based implementation for maximum throughput and minimal latency
- **Simple synchronous API**: Easy-to-use Portal class with static methods for server/client creation. WebSocket-style messaging.

## Upcoming roadmap

- **TODO: Improved NAT traversal**: Handle more complex client-side NATs using port scanning + birthday technique. Currently only supports clients behind "easy" NATs.
- **TODO: Shared server certificates**: Use a modal.Dict to share server/client certificates, to mutually validate identity.

## Installation

```bash
# Install from PyPi (only certain wheels built)
pip install quic-portal
```

```bash
# Install from source (requires Rust toolchain)
git clone <repository>
cd quic-portal
pip install .
```

## Quick Start

### Usage with Modal

```python
import modal
from quic_portal import Portal

app = modal.App("my-quic-app")

@app.function()
def server_function(coord_dict: modal.Dict):
    # Create server with automatic NAT traversal
    portal = Portal.create_server(dict=coord_dict, local_port=5555)
    
    # Receive and echo messages
    while True:
        data = portal.recv(timeout_ms=10000)
        if data:
            message = data.decode("utf-8")
            print(f"Received: {message}")
            portal.send(f"Echo: {message}".encode("utf-8"))

@app.function()
def client_function(coord_dict: modal.Dict):
    # Create client with automatic NAT traversal
    portal = Portal.create_client(dict=coord_dict, local_port=5556)
    
    # Send messages
    portal.send(b"Hello, QUIC!")
    response = portal.recv(timeout_ms=5000)
    if response:
        print(f"Got response: {response.decode('utf-8')}")

@app.local_entrypoint()
def main(local: bool = False):
    # Create coordination dict
    with modal.Dict.ephemeral() as coord_dict:
        # Start server
        server_task = server_function.spawn(coord_dict)
        
        # Run client
        if local:
            # Run test between local environment and remote container.
            client_function.local(coord_dict)
        else:
            # Run test between two containers.
            client_function.remote(coord_dict)
        
        server_task.cancel()
```

### Manual NAT Traversal

For advanced use cases where you handle NAT traversal yourself, or the server has a public IP:

```python
from quic_portal import Portal

# After NAT hole punching is complete...
# Server side
server = Portal()
server.listen(5555)

# Client side  
client = Portal()
client.connect("server_ip", 5555, 5556)

# WebSocket-style messaging
client.send(b"Hello!")
response = server.recv(timeout_ms=1000)
```

## API Reference

### Portal Class

#### Static Methods

##### `Portal.create_server(dict, local_port=5555, stun_server=("stun.ekiga.net", 3478), punch_timeout=15)`

Create a server portal with automatic NAT traversal. **Synchronous operation.**

**Parameters:**
- `dict` (modal.Dict or dict): Modal Dict or regular dict for peer coordination
- `local_port` (int): Local port for QUIC server (default: 5555)
- `stun_server` (tuple): STUN server for NAT discovery (default: ("stun.ekiga.net", 3478))
- `punch_timeout` (int): Timeout in seconds for NAT punching (default: 15)

**Returns:** Connected Portal instance ready for communication

##### `Portal.create_client(dict, local_port=5556, stun_server=("stun.ekiga.net", 3478), punch_timeout=15)`

Create a client portal with automatic NAT traversal. **Synchronous operation.**

**Parameters:**
- `dict` (modal.Dict or dict): Modal Dict or regular dict for peer coordination (must be same as server)
- `local_port` (int): Local port for QUIC client (default: 5556)
- `stun_server` (tuple): STUN server for NAT discovery (default: ("stun.ekiga.net", 3478))
- `punch_timeout` (int): Timeout in seconds for NAT punching (default: 15)

**Returns:** Connected Portal instance ready for communication

#### Instance Methods

##### `send(data: Union[bytes, str]) -> None`

Send data over QUIC connection (WebSocket-style). **Synchronous operation.**

##### `recv(timeout_ms: Optional[int] = None) -> Optional[bytes]`

Receive data from QUIC connection. Blocks until message arrives or timeout. **Synchronous operation.**

**Parameters:**
- `timeout_ms` (int, optional): Timeout in milliseconds (None for blocking)

**Returns:** Received data as bytes, or None if timeout

##### `connect(server_ip: str, server_port: int, local_port: int) -> None`

Connect to a QUIC server (for manual NAT traversal). **Synchronous operation.**

**Parameters:**
- `server_ip` (str): Server IP address
- `server_port` (int): Server port
- `local_port` (int): Local port to bind to

##### `listen(local_port: int) -> None`

Start QUIC server and wait for connection (for manual NAT traversal). **Synchronous operation.**

**Parameters:**
- `local_port` (int): Local port to bind to

##### `is_connected() -> bool`

Check if connected to peer.

##### `close() -> None`

Close the connection and clean up resources.

## Examples

See the `examples/` directory for complete working examples:

- `modal_simple.py` - Basic server/client communication
- `modal_benchmark.py` - Performance benchmarking

## Requirements

- Python 3.9+
- Modal (for automatic NAT traversal)
- Rust toolchain (for building from source)

## Third-party Libraries
This project uses code from:
- `pynat` by Ariel Antonitis, licensed under MIT License

## License

MIT License 
