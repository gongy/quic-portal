import logging
import socket as socketlib
import time
from typing import Optional, Union, Any

from ._core import QuicPortal as _QuicPortal, QuicTransportOptions as _QuicTransportOptions
from .exceptions import PortalError, ConnectionError
from .nat import get_stun_response

# Simple logger setup.
logger = logging.getLogger("quic_portal")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] [%(asctime)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False

# Enable debug logging for Quinn specifically.
quinn_logger = logging.getLogger("quinn")
quinn_logger.setLevel(logging.WARN)
if not quinn_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] [%(asctime)s] %(levelname)s: %(message)s"))
    quinn_logger.addHandler(handler)
    logger.propagate = False

quinn_logger = logging.getLogger("quinn_proto")
if not quinn_logger.handlers:
    quinn_logger.setLevel(logging.WARN)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] [%(asctime)s] %(levelname)s: %(message)s"))
    quinn_logger.addHandler(handler)
    logger.propagate = False

quinn_logger = logging.getLogger("quinn_udp")
quinn_logger.setLevel(logging.WARN)
if not quinn_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(name)s] [%(asctime)s] %(levelname)s: %(message)s"))
    quinn_logger.addHandler(handler)
    logger.propagate = False


class QuicTransportOptions:
    """Configuration options for QUIC transport."""

    def __init__(
        self,
        max_idle_timeout_secs: int = 10,
        congestion_controller_type: str = "cubic",
        initial_window: int = 1024 * 1024,  # 1MiB
        keep_alive_interval_secs: int = 2,
    ):
        """
        Initialize QUIC transport options.

        Args:
            max_idle_timeout_secs: Maximum idle timeout in seconds
            congestion_controller_type: Congestion controller type ("cubic", "bbr", "fixed")
            initial_window: Initial window size in bytes
            keep_alive_interval_secs: Keep alive interval in seconds
        """
        self._core = _QuicTransportOptions()
        self._core.max_idle_timeout_secs = max_idle_timeout_secs
        self._core.congestion_controller_type = congestion_controller_type
        self._core.initial_window = initial_window
        self._core.keep_alive_interval_secs = keep_alive_interval_secs

    @property
    def max_idle_timeout_secs(self) -> int:
        return self._core.max_idle_timeout_secs

    @max_idle_timeout_secs.setter
    def max_idle_timeout_secs(self, value: int):
        self._core.max_idle_timeout_secs = value

    @property
    def congestion_controller_type(self) -> str:
        return self._core.congestion_controller_type

    @congestion_controller_type.setter
    def congestion_controller_type(self, value: str):
        self._core.congestion_controller_type = value

    @property
    def initial_window(self) -> int:
        return self._core.initial_window

    @initial_window.setter
    def initial_window(self, value: int):
        self._core.initial_window = value

    @property
    def keep_alive_interval_secs(self) -> int:
        return self._core.keep_alive_interval_secs

    @keep_alive_interval_secs.setter
    def keep_alive_interval_secs(self, value: int):
        self._core.keep_alive_interval_secs = value


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

        # Only set for client end of portal.
        self.server_ip = None
        self.server_port = None

    @staticmethod
    def create_server(
        dict: Any,
        local_port: int = 5555,
        stun_servers: list[tuple[str, int]] = [
            ("stun.ekiga.net", 3478),
            ("stun.l.google.com", 19302),
        ],
        punch_timeout: int = 15,
        transport_options: Optional[QuicTransportOptions] = None,
    ) -> "Portal":
        """
        Create a QUIC server with automatic NAT traversal.

        Args:
            dict: Modal Dict or dict-like object for coordination
            local_port: Local port to bind to
            stun_servers: List of STUN servers for NAT discovery
            punch_timeout: Timeout for NAT punching in seconds
            transport_options: QUIC transport configuration options

        Returns:
            Connected Portal instance
        """

        sock = get_socket(local_port)

        try:
            # Get external IP/port via STUN
            pub_addrs = Portal._get_ext_addr(sock, stun_servers)
            logger.debug(f"[server] Public endpoints: {pub_addrs}")

            # Register with coordination dict and wait for client
            client_endpoints = []
            while not client_endpoints:
                pub_addrs = Portal._get_ext_addr(sock, stun_servers)
                logger.debug(f"[server] Public endpoints: {pub_addrs}")

                dict["server"] = pub_addrs
                if "client" in dict:
                    client_endpoint = dict["client"]
                    if isinstance(client_endpoint[0], str):
                        # Old version added (ip, port) tuple
                        client_endpoints.append(client_endpoint)
                    else:
                        # New version added list of (ip, port) tuples
                        client_endpoints.extend(client_endpoint)

                    logger.debug(f"[server] Got client endpoints: {client_endpoints}")
                    break
                logger.debug("[server] Waiting for client to register...")
                time.sleep(0.2)

            attempts = 0

            # Use these to establish mappings at server-side NAT
            client_addrs_to_hit = set(client_endpoints)

            # Punch NAT
            punch_success = False
            start_time = time.time()
            while time.time() - start_time < punch_timeout:
                for endpoint in client_addrs_to_hit:
                    logger.debug(f"[server] Punching to {endpoint}")
                    sock.sendto(b"punch", endpoint)

                try:
                    data, addr = sock.recvfrom(1024)
                    if data == b"punch":
                        logger.debug(f"[server] Received punch from client at {addr}")
                        sock.sendto(b"punch-ack", addr)
                        punch_success = True
                        break
                except socketlib.timeout:
                    attempts += 1
                    client_ips = set(addr[0] for addr in client_addrs_to_hit)

                    if attempts == 1:
                        for client_ip in client_ips:
                            for _port in range(1000, 9999):
                                client_addrs_to_hit.add((client_ip, _port))
                    elif attempts == 2:
                        for client_ip in client_ips:
                            for _port in range(1000, 65535):
                                client_addrs_to_hit.add((client_ip, _port))

                    continue

            if not punch_success:
                raise ConnectionError("Failed to punch NAT with client")

            # Close UDP socket before QUIC can use the port
            sock.close()
            logger.info("[server] nat traversal successful")

            # Wait a moment to ensure socket is properly closed
            time.sleep(0.1)

            # Create Portal and start listening
            portal = Portal()
            portal.listen(local_port, transport_options or QuicTransportOptions())

            return portal

        except Exception as e:
            sock.close()
            raise ConnectionError(f"Server creation failed: {e}")

    @staticmethod
    def create_client(
        dict: Any,
        local_port: int = 5556,
        stun_servers: list[tuple[str, int]] = [
            ("stun.ekiga.net", 3478),
            ("stun.l.google.com", 19302),
        ],
        punch_timeout: int = 15,
        transport_options: Optional[QuicTransportOptions] = None,
    ) -> "Portal":
        """
        Create a QUIC client with automatic NAT traversal.

        Args:
            dict: Modal Dict or dict-like object for coordination
            local_port: Local port to bind to
            stun_servers: List of STUN servers for NAT discovery
            punch_timeout: Timeout for NAT punching in seconds
            transport_options: QUIC transport configuration options

        Returns:
            Connected Portal instance
        """

        sock = get_socket(local_port)

        try:
            # Register with coordination dict and wait for server
            server_endpoints = []
            while not server_endpoints:
                pub_ip, pub_port = Portal._get_ext_addr(sock, stun_servers)
                logger.debug(f"[client] Public endpoint: {pub_ip}:{pub_port}")

                dict["client"] = (pub_ip, pub_port)
                if "server" in dict:
                    server_endpoint = dict["server"]
                    if isinstance(server_endpoint[0], str):
                        # Old version added (ip, port) tuple
                        server_endpoints.append(server_endpoint)
                    else:
                        # New version added list of (ip, port) tuples
                        server_endpoints.extend(server_endpoint)
                    logger.debug(f"[client] Got server endpoint: {server_endpoint}")
                    break
                logger.debug("[client] Waiting for server to register...")
                time.sleep(0.2)

            # Server should be fairly stable, so just use first one.
            server_ip, server_port = server_endpoints[0]

            # Punch NAT
            punch_success = False
            start_time = time.time()
            while time.time() - start_time < punch_timeout:
                logger.debug(f"[client] Punching to server at {server_ip}:{server_port}")
                sock.sendto(b"punch", (server_ip, server_port))
                try:
                    data, addr = sock.recvfrom(1024)
                    if data == b"punch-ack" and addr[0] == server_ip and addr[1] == server_port:
                        logger.debug("[client] Received punch-ack from server")
                        punch_success = True
                        break
                    else:
                        logger.debug(
                            f"[client] Received punch-ack from unexpected source at {addr}. Continuing."
                        )
                        continue
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
            portal.connect(server_ip, server_port, local_port, transport_options or QuicTransportOptions())

            return portal

        except Exception as e:
            sock.close()
            raise ConnectionError(f"Client creation failed: {e}")

    @staticmethod
    def _get_ext_addr(sock, stun_servers):
        """Get external IP and port using STUN."""
        responses = [get_stun_response(sock, stun_server) for stun_server in stun_servers]
        return [(response["ext_ip"], response["ext_port"]) for response in responses]

    def connect(self, server_ip: str, server_port: int, local_port: Optional[int] = None, transport_options: Optional[QuicTransportOptions] = None) -> None:
        """
        Connect to a QUIC server (after NAT traversal is complete).

        Args:
            server_ip: Server IP address
            server_port: Server port
            local_port: Local port to bind to (required)
            transport_options: QUIC transport configuration options
        """
        if local_port is None:
            raise ValueError("local_port is required for connect()")

        try:
            core_options = (transport_options or QuicTransportOptions())._core
            self._core.connect(server_ip, server_port, local_port, core_options)
            self._connected = True
            self.server_ip = server_ip
            self.server_port = server_port
            logger.debug(f"[PORTAL] QUIC connection established to {server_ip}:{server_port}")
        except Exception as e:
            raise ConnectionError(f"Failed to connect: {e}") from e

    def listen(self, local_port: Optional[int] = None, transport_options: Optional[QuicTransportOptions] = None) -> None:
        """
        Start QUIC server and wait for connection (after NAT traversal is complete).

        Args:
            local_port: Local port to bind to (required)
            transport_options: QUIC transport configuration options
        """
        if local_port is None:
            raise ValueError("local_port is required for listen()")

        try:
            core_options = (transport_options or QuicTransportOptions())._core
            self._core.listen(local_port, core_options)
            self._connected = True
            logger.debug(f"[PORTAL] QUIC server started on port {local_port}")
        except Exception as e:
            raise ConnectionError(f"Failed to start server: {e}") from e

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
