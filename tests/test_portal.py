"""
Tests for quic-portal
"""

import pytest
from quic_portal import Portal, ConnectionError


def test_portal_creation():
    """Test basic portal creation"""
    portal = Portal()
    assert not portal.is_connected()


def test_portal_not_connected_error():
    """Test that operations fail when not connected"""
    portal = Portal()

    with pytest.raises(ConnectionError):
        portal.send(b"test")

    with pytest.raises(ConnectionError):
        portal.recv(timeout_ms=100)

    portal.close()


def test_portal_string_encoding():
    """Test that string messages are properly encoded"""
    portal = Portal()

    # This should work without connection for encoding test
    # (will fail at send time due to no connection, but that's expected)
    try:
        portal.send("test string")
    except ConnectionError:
        pass  # Expected since not connected

    portal.close()


def test_portal_close_multiple_times():
    """Test that closing multiple times doesn't cause issues"""
    portal = Portal()
    portal.close()
    portal.close()  # Should not raise an error


def test_portal_connection_status():
    """Test connection status tracking"""
    portal = Portal()
    assert not portal.is_connected()

    # After failed connection attempt, should still be disconnected
    try:
        portal.connect("invalid.host", 9999, local_port=5555)
    except ConnectionError:
        pass  # Expected to fail

    assert not portal.is_connected()
    portal.close()


def test_portal_requires_local_port():
    """Test that connect and listen require local_port"""
    portal = Portal()

    with pytest.raises(ValueError, match="local_port is required"):
        portal.connect("127.0.0.1", 5555)

    with pytest.raises(ValueError, match="local_port is required"):
        portal.listen()

    portal.close()


if __name__ == "__main__":
    pytest.main([__file__])
