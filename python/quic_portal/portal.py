import logging
import socket as socketlib
import time
from typing import Optional, Union, Any

from ._core import QuicPortal as _QuicPortal
from .exceptions import PortalError, ConnectionError
from .nat import get_stun_response

# Simple logger setup
logger = logging.getLogger("quic_portal")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def get_socket(local_port: int) -> socketlib.socket:
    """Get a socket with large buffers and non-blocking behavior."""
    sock = socketlib.socket(socketlib.AF_INET, socketlib.SOCK_DGRAM)
    sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_RCVBUF, 64 * 1024 * 1024)
    sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_SNDBUF, 64 * 1024 * 1024)
    sock.setsockopt(socketlib.SOL_SOCKET, socketlib.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", local_port))
    sock.settimeout(0.1)  # 100ms timeout for non-blocking behavior during punching.
    return sock


class Portal:
    """
    High-level QUIC portal for bidirectional communication.

    Can be used directly after NAT traversal, or use the static methods
    create_client() and create_server() for automatic NAT traversal.

    Example (manual):
        # After NAT hole punching is complete...
        portal = Portal()
        portal.connect("192.168.1.100", 5555, local_port=5556)

        # Send messages (WebSocket-style)
        portal.send(b"Hello, QUIC!")

        # Receive messages (blocks until message arrives)
        data = portal.recv(timeout_ms=1000)
        if data:
            print(f"Received: {data}")

    Example (automatic NAT traversal):
        import modal

        # Server side
        with modal.Dict.ephemeral() as coord_dict:
            server_portal = Portal.create_server(dict=coord_dict, local_port=5555)

        # Client side
        with modal.Dict.ephemeral() as coord_dict:
            client_portal = Portal.create_client(dict=coord_dict, local_port=5556)
    """

    def __init__(self):
        self._core = _QuicPortal()
        self._connected = False

    @staticmethod
    def create_server(
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

        sock = get_socket(local_port)

        try:
            # Get external IP/port via STUN
            pub_ip, pub_port = Portal._get_ext_addr(sock, stun_server)
            logger.debug(f"[server] Public endpoint: {pub_ip}:{pub_port}")

            # Register with coordination dict and wait for client
            client_endpoint = None
            while not client_endpoint:
                pub_ip, pub_port = Portal._get_ext_addr(sock, stun_server)
                logger.debug(f"[server] Public endpoint: {pub_ip}:{pub_port}")

                dict["server"] = (pub_ip, pub_port)
                if "client" in dict:
                    client_endpoint = dict["client"]
                    logger.debug(f"[server] Got client endpoint: {client_endpoint}")
                    break
                logger.debug("[server] Waiting for client to register...")
                time.sleep(0.2)

            client_ip, client_port = client_endpoint

            # Punch NAT
            punch_success = False
            start_time = time.time()
            while time.time() - start_time < punch_timeout:
                logger.debug(f"[server] Punching to {client_ip}:{client_port}")
                sock.sendto(b"punch", (client_ip, client_port))
                try:
                    data, addr = sock.recvfrom(1024)
                    if data == b"punch" and addr[0] == client_ip:
                        logger.debug(f"[server] Received punch from client at {addr}")
                        sock.sendto(b"punch-ack", addr)
                        punch_success = True
                        break
                    elif data == b"punch" and addr[0] != client_ip:
                        logger.debug(f"[server] Received punch from unexpected source at {addr}")
                        logger.debug("[server] Assuming the new source is the client")
                        client_ip, client_port = addr
                        sock.sendto(b"punch-ack", addr)
                        punch_success = True
                        break
                except socketlib.timeout:
                    continue

            if not punch_success:
                raise ConnectionError("Failed to punch NAT with client")

            # Close UDP socket before QUIC can use the port
            sock.close()
            logger.info("[server] nat traversal successful")

            # Wait a moment to ensure socket is properly closed
            time.sleep(0.05)

            # Create Portal and start listening
            portal = Portal()
            portal.listen(local_port)

            return portal

        except Exception as e:
            sock.close()
            raise ConnectionError(f"Server creation failed: {e}")

    @staticmethod
    def create_client(
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

        sock = get_socket(local_port)

        try:
            # Register with coordination dict and wait for server
            server_endpoint = None
            while not server_endpoint:
                pub_ip, pub_port = Portal._get_ext_addr(sock, stun_server)
                logger.debug(f"[client] Public endpoint: {pub_ip}:{pub_port}")

                dict["client"] = (pub_ip, pub_port)
                if "server" in dict:
                    server_endpoint = dict["server"]
                    logger.debug(f"[client] Got server endpoint: {server_endpoint}")
                    break
                logger.debug("[client] Waiting for server to register...")
                time.sleep(0.2)

            server_ip, server_port = server_endpoint

            # Punch NAT
            punch_success = False
            start_time = time.time()
            while time.time() - start_time < punch_timeout:
                logger.debug(f"[client] Punching to server at {server_ip}:{server_port}")
                sock.sendto(b"punch", (server_ip, server_port))
                try:
                    data, addr = sock.recvfrom(1024)
                    if data == b"punch-ack" and addr[0] == server_ip:
                        logger.debug("[client] Received punch-ack from server")
                        punch_success = True
                        break
                except socketlib.timeout:
                    continue

            if not punch_success:
                raise ConnectionError("Failed to punch NAT with server")

            logger.debug("[client] Punch successful, establishing QUIC connection")

            # Close UDP socket before QUIC can use the port
            sock.close()
            logger.info("[client] nat traversal successful")

            # Wait a moment to ensure socket is properly closed
            time.sleep(0.05)

            # Create Portal and connect
            portal = Portal()
            portal.connect(server_ip, server_port, local_port)

            return portal

        except Exception as e:
            sock.close()
            raise ConnectionError(f"Client creation failed: {e}")

    @staticmethod
    def _get_ext_addr(sock, stun_server):
        """Get external IP and port using STUN."""
        response = get_stun_response(sock, stun_server)
        return response["ext_ip"], response["ext_port"]

    def connect(self, server_ip: str, server_port: int, local_port: Optional[int] = None) -> None:
        """
        Connect to a QUIC server (after NAT traversal is complete).

        Args:
            server_ip: Server IP address
            server_port: Server port
            local_port: Local port to bind to (required)
        """
        if local_port is None:
            raise ValueError("local_port is required for connect()")

        try:
            self._core.connect(server_ip, server_port, local_port)
            self._connected = True
            logger.debug(f"[PORTAL] QUIC connection established to {server_ip}:{server_port}")
        except Exception as e:
            raise ConnectionError(f"Failed to connect: {e}")

    def listen(self, local_port: Optional[int] = None) -> None:
        """
        Start QUIC server and wait for connection (after NAT traversal is complete).

        Args:
            local_port: Local port to bind to (required)
        """
        if local_port is None:
            raise ValueError("local_port is required for listen()")

        try:
            self._core.listen(local_port)
            self._connected = True
            logger.debug(f"[PORTAL] QUIC server started on port {local_port}")
        except Exception as e:
            raise ConnectionError(f"Failed to start server: {e}")

    def send(self, data: Union[bytes, str]) -> None:
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

    def recv(self, timeout_ms: Optional[int] = None) -> Optional[bytes]:
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

    def close(self) -> None:
        """Close all connections."""
        self._core.close()
        self._connected = False
