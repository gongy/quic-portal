"""
High-level Python API for QUIC Portal
"""

import asyncio
import socket as socketlib
import uuid
from typing import Optional, Union, Any

from ._core import QuicPortal as _QuicPortal
from .exceptions import PortalError, ConnectionError


class Portal:
    """
    High-level QUIC portal for bidirectional communication.

    Can be used directly after NAT traversal, or use the static methods
    create_client() and create_server() for automatic NAT traversal.

    Example (manual):
        # After NAT hole punching is complete...
        portal = Portal()
        await portal.connect("192.168.1.100", 5555, local_port=5556)

        # Send messages (WebSocket-style)
        await portal.send(b"Hello, QUIC!")

        # Receive messages (blocks until message arrives)
        data = await portal.recv(timeout_ms=1000)
        if data:
            print(f"Received: {data}")

    Example (automatic NAT traversal):
        import modal

        # Server side
        async with modal.Dict.ephemeral() as coord_dict:
            server_portal = await Portal.create_server(dict=coord_dict, local_port=5555)

        # Client side
        async with modal.Dict.ephemeral() as coord_dict:
            client_portal = await Portal.create_client(dict=coord_dict, local_port=5556)
    """

    def __init__(self):
        self._core = _QuicPortal()
        self._connected = False

    @staticmethod
    async def create_server(
        dict: Any,
        local_port: int = 5555,
        stun_server: tuple[str, int] = ("stun.ekiga.net", 3478),
        punch_timeout: int = 15,
    ) -> "Portal":
        """
        Create a QUIC server with automatic NAT traversal.

        Args:
            dict: Modal Dict or dict-like object for coordination
            local_port: Local port to bind to
            stun_server: STUN server for NAT discovery
            punch_timeout: Timeout for NAT punching in seconds

        Returns:
            Connected Portal instance
        """
        # Initialize socket with large buffers
        sock = socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM)
        sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_RCVBUF, 64 * 1024 * 1024)
        sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_SNDBUF, 64 * 1024 * 1024)
        sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", local_port))
        sock.setblocking(False)

        try:
            # Get external IP/port via STUN
            pub_ip, pub_port = await Portal._get_ext_addr(sock, stun_server)
            print(f"[PORTAL SERVER] Public endpoint: {pub_ip}:{pub_port}")

            # Register with coordination dict and wait for client
            client_endpoint = None
            while not client_endpoint:
                pub_ip, pub_port = await Portal._get_ext_addr(sock, stun_server)
                print(f"[PORTAL SERVER] Public endpoint: {pub_ip}:{pub_port}")

                # Write server endpoint to dict
                if hasattr(dict, "put"):
                    # Modal Dict
                    await dict.put.aio(key="server", value=(pub_ip, pub_port))
                    client_endpoint = await dict.get.aio(key="client")
                else:
                    # Regular dict
                    dict["server"] = (pub_ip, pub_port)
                    client_endpoint = dict.get("client")

                if client_endpoint:
                    print(f"[PORTAL SERVER] Got client endpoint: {client_endpoint}")
                    break
                print("[PORTAL SERVER] Waiting for client to register...")
                await asyncio.sleep(0.2)

            client_ip, client_port = client_endpoint

            # Punch NAT
            punch_success = False
            for _ in range(punch_timeout * 5):  # 5 attempts per second
                print(f"[PORTAL SERVER] Punching to {client_ip}:{client_port}")
                sock.sendto(b"punch", (client_ip, client_port))
                try:
                    data, addr = await asyncio.wait_for(
                        asyncio.get_event_loop().sock_recvfrom(sock, 1024), timeout=0.1
                    )
                    if data == b"punch" and addr[0] == client_ip:
                        print(f"[PORTAL SERVER] Received punch from client at {addr}")
                        sock.sendto(b"punch-ack", addr)
                        punch_success = True
                        break
                    elif data == b"punch" and addr[0] != client_ip:
                        print(f"[PORTAL SERVER] Received punch from unexpected source at {addr}")
                        print(f"[PORTAL SERVER] Assuming the new source is the client")
                        client_ip, client_port = addr
                        sock.sendto(b"punch-ack", addr)
                        punch_success = True
                        break
                except (asyncio.TimeoutError, BlockingIOError):
                    continue

            if not punch_success:
                raise ConnectionError("Failed to punch NAT with client")

            # Close UDP socket before QUIC can use the port
            sock.close()
            print("[PORTAL SERVER] UDP socket closed, preparing QUIC server")

            # Wait a moment to ensure socket is properly closed
            await asyncio.sleep(0.2)

            # Create Portal and start listening
            portal = Portal()
            await portal.listen(local_port)

            return portal

        except Exception as e:
            sock.close()
            raise ConnectionError(f"Server creation failed: {e}")

    @staticmethod
    async def create_client(
        dict: Any,
        local_port: int = 5556,
        stun_server: tuple[str, int] = ("stun.ekiga.net", 3478),
        punch_timeout: int = 15,
    ) -> "Portal":
        """
        Create a QUIC client with automatic NAT traversal.

        Args:
            dict: Modal Dict or dict-like object for coordination
            local_port: Local port to bind to
            stun_server: STUN server for NAT discovery
            punch_timeout: Timeout for NAT punching in seconds

        Returns:
            Connected Portal instance
        """

        # Initialize socket with large buffers
        sock = socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM)
        sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_RCVBUF, 64 * 1024 * 1024)
        sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_SNDBUF, 64 * 1024 * 1024)
        sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", local_port))
        sock.setblocking(False)

        try:
            # Register with coordination dict and wait for server
            server_endpoint = None
            while not server_endpoint:
                pub_ip, pub_port = await Portal._get_ext_addr(sock, stun_server)
                print(f"[PORTAL CLIENT] Public endpoint: {pub_ip}:{pub_port}")

                # Write client endpoint to dict
                if hasattr(dict, "put"):
                    # Modal Dict
                    await dict.put.aio(key="client", value=(pub_ip, pub_port))
                    server_endpoint = await dict.get.aio(key="server")
                else:
                    # Regular dict
                    dict["client"] = (pub_ip, pub_port)
                    server_endpoint = dict.get("server")

                if server_endpoint:
                    print(f"[PORTAL CLIENT] Got server endpoint: {server_endpoint}")
                    break
                print("[PORTAL CLIENT] Waiting for server to register...")
                await asyncio.sleep(0.2)

            server_ip, server_port = server_endpoint

            # Punch NAT
            punch_success = False
            for _ in range(punch_timeout * 5):  # 5 attempts per second
                print(f"[PORTAL CLIENT] Punching to server at {server_ip}:{server_port}")
                sock.sendto(b"punch", (server_ip, server_port))
                try:
                    data, addr = await asyncio.wait_for(
                        asyncio.get_event_loop().sock_recvfrom(sock, 1024), timeout=0.1
                    )
                    if data == b"punch-ack" and addr[0] == server_ip:
                        print(f"[PORTAL CLIENT] Received punch-ack from server")
                        punch_success = True
                        break
                except (asyncio.TimeoutError, BlockingIOError):
                    continue

            if not punch_success:
                raise ConnectionError("Failed to punch NAT with server")

            print("[PORTAL CLIENT] Punch successful, establishing QUIC connection")

            # Close UDP socket before QUIC can use the port
            sock.close()
            print("[PORTAL CLIENT] UDP socket closed, preparing QUIC connection")

            # Wait a moment to ensure socket is properly closed
            await asyncio.sleep(0.2)

            # Create Portal and connect
            portal = Portal()
            await portal.connect(server_ip, server_port, local_port)

            return portal

        except Exception as e:
            sock.close()
            raise ConnectionError(f"Client creation failed: {e}")

    @staticmethod
    async def _get_ext_addr(sock, stun_server):
        """Get external IP and port using STUN."""
        try:
            from pynat import get_stun_response

            response = get_stun_response(sock, stun_server)
            return response["ext_ip"], response["ext_port"]
        except ImportError:
            raise PortalError(
                "pynat package required for NAT traversal. Install with: pip install pynat"
            )

    async def connect(
        self, server_ip: str, server_port: int, local_port: Optional[int] = None
    ) -> None:
        """
        Connect to a QUIC server (after NAT traversal is complete).

        Args:
            server_ip: Server IP address
            server_port: Server port
            local_port: Local port (defaults to the one set in __init__)
        """
        if local_port is None:
            local_port = self._local_port

        try:
            self._core.connect(server_ip, server_port, local_port)
            self._connected = True
            print(f"[PORTAL] QUIC connection established to {server_ip}:{server_port}")
        except Exception as e:
            raise ConnectionError(f"Failed to connect: {e}")

    async def listen(self, local_port: Optional[int] = None) -> None:
        """
        Start QUIC server and wait for connection (after NAT traversal is complete).

        Args:
            local_port: Local port (defaults to the one set in __init__)
        """
        if local_port is None:
            local_port = self._local_port

        try:
            self._core.listen(local_port)
            self._connected = True
            print(f"[PORTAL] QUIC server started on port {local_port}")
        except Exception as e:
            raise ConnectionError(f"Failed to start server: {e}")

    async def send(self, data: Union[bytes, str]) -> None:
        """
        Send data over QUIC (WebSocket-style: no response expected).

        Args:
            data: Data to send (bytes or string)
        """
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

        if isinstance(data, str):
            data = data.encode("utf-8")

        self._core.send(data)

    async def recv(self, timeout_ms: Optional[int] = None) -> Optional[bytes]:
        """
        Receive data from QUIC connection (WebSocket-style: blocks until message arrives).

        Args:
            timeout_ms: Timeout in milliseconds (None for blocking)

        Returns:
            Received data as bytes, or None if timeout
        """
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

        return self._core.recv(timeout_ms)

    def is_connected(self) -> bool:
        """Check if connected to QUIC server."""
        return self._core.is_connected()

    async def close(self) -> None:
        """Close all connections."""
        self._core.close()
        self._connected = False
