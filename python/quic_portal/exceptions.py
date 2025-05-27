"""
Exception classes for quic-portal
"""


class PortalError(Exception):
    """Base exception for all portal errors"""
    pass


class ConnectionError(PortalError):
    """Raised when QUIC connection fails"""
    pass 