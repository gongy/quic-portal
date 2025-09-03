"""
QUIC Portal - High-performance QUIC communication with NAT traversal
"""

from .constants import VERSION
from .portal import Portal, QuicTransportOptions
from .exceptions import PortalError, ConnectionError

__all__ = ["Portal", "QuicTransportOptions", "PortalError", "ConnectionError"]
__version__ = VERSION
