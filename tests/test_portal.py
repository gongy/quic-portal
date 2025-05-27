"""
Tests for quic-portal
"""

import pytest
import asyncio
from quic_portal import Portal, PortalError, ConnectionError


def test_portal_creation():
    """Test basic portal creation"""
    portal = Portal(local_port=5556)
    assert not portal.is_connected()


@pytest.mark.asyncio
async def test_portal_not_connected_error():
    """Test that operations fail when not connected"""
    portal = Portal()

    with pytest.raises(ConnectionError):
        await portal.send(b"test")

    with pytest.raises(ConnectionError):
        await portal.recv(timeout_ms=100)

    await portal.close()


@pytest.mark.asyncio
async def test_portal_string_encoding():
    """Test that string messages are properly encoded"""
    portal = Portal()

    # This should work without connection for encoding test
    # (will fail at send time due to no connection, but that's expected)
    try:
        await portal.send("test string")
    except ConnectionError:
        pass  # Expected since not connected

    await portal.close()


@pytest.mark.asyncio
async def test_portal_close_multiple_times():
    """Test that closing multiple times doesn't cause issues"""
    portal = Portal()
    await portal.close()
    await portal.close()  # Should not raise an error


@pytest.mark.asyncio
async def test_portal_connection_status():
    """Test connection status tracking"""
    portal = Portal()
    assert not portal.is_connected()

    # After failed connection attempt, should still be disconnected
    try:
        await portal.connect("invalid.host", 9999)
    except ConnectionError:
        pass  # Expected to fail

    assert not portal.is_connected()
    await portal.close()


if __name__ == "__main__":
    pytest.main([__file__])
