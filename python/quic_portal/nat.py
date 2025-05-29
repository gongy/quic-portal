# NAT traversal utility functions.

# Portions of this code adapted from https://github.com/aarant/pynat
# Copyright (c) 2018 Ariel Antonitis
# Licensed under MIT License

import random
import codecs
import socket


# Non-NAT network topologies
BLOCKED = "Blocked"
OPEN = "Open"
UDP_FIREWALL = "UDP Firewall"
# NAT topologies
FULL_CONE = "Full-cone NAT"
RESTRICTED_CONE = "Restricted-cone NAT"
RESTRICTED_PORT = "Restricted-port NAT"
SYMMETRIC = "Symmetric NAT"

# Stun message types
BIND_REQUEST_MSG = b"\x00\x01"
BIND_RESPONSE_MSG = b"\x01\x01"
MAGIC_COOKIE = b"\x21\x12\xA4\x42"

# Stun attributes
MAPPED_ADDRESS = b"\x00\x01"
RESPONSE_ADDRESS = b"\x00\x02"
CHANGE_REQUEST = b"\x00\x03"
SOURCE_ADDRESS = b"\x00\x04"
CHANGED_ADDRESS = b"\x00\x05"
XOR_MAPPED_ADDRESS = b"\x00\x20"

# List of classic STUN servers
STUN_SERVERS = [
    ("stun.ekiga.net", 3478),
    ("stun.l.google.com", 19302),
]


def ORD(ch):
    return ch if isinstance(ch, int) else ord(ch)


def long_to_bytes(n: int, length: int) -> bytes:
    """
    Convert a long integer to a byte string.
    """
    return bytes(bytearray((n >> i * 8) & 0xFF for i in range(length - 1, -1, -1)))


def send_stun_message(sock, addr, msg_type, trans_id=None, send_data=b""):
    """
    Send a STUN message to a server, with optional extra data.
    """
    if trans_id is None:
        trans_id = long_to_bytes(random.getrandbits(128), 16)
    msg_len = long_to_bytes(len(send_data), 2)
    data = msg_type + msg_len + trans_id + send_data
    sock.sendto(data, addr)
    return trans_id


def get_stun_response(sock, addr, trans_id=None, send_data=b"", max_timeouts=6):
    """
    Get a STUN Binding response from a server, with optional extra data.
    """
    timeouts = 0
    response = None
    old_timeout = sock.gettimeout()
    sock.settimeout(0.5)
    while timeouts < max_timeouts:
        try:
            trans_id = send_stun_message(sock, addr, BIND_REQUEST_MSG, trans_id, send_data)
            recv, addr = sock.recvfrom(2048)  # TODO: Why 2048
        except socket.timeout:
            timeouts += 1
            continue
        else:
            # Too short, not a valid message
            if len(recv) < 20:
                continue
            msg_type, recv_trans_id, attrs = recv[:2], recv[4:20], recv[20:]
            msg_len = int(codecs.encode(recv[2:4], "hex"), 16)
            if msg_len != len(attrs):
                continue
            if msg_type != BIND_RESPONSE_MSG:
                continue
            if recv_trans_id != trans_id:
                continue
            response = {}
            i = 0
            while i < msg_len:
                attr_type, attr_length = (
                    attrs[i : i + 2],
                    int(codecs.encode(attrs[i + 2 : i + 4], "hex"), 16),
                )
                attr_value = attrs[i + 4 : i + 4 + attr_length]
                i += 4 + attr_length
                if attr_length % 4 != 0:  # If not on a 32-bit boundary, add padding bytes
                    i += 4 - (attr_length % 4)
                if attr_type in [MAPPED_ADDRESS, SOURCE_ADDRESS, CHANGED_ADDRESS]:
                    family, port = (
                        ORD(attr_value[1]),
                        int(codecs.encode(attr_value[2:4], "hex"), 16),
                    )
                    if family == 0x01:  # IPv4
                        ip = socket.inet_ntop(socket.AF_INET, attr_value[4:8])
                        if attr_type == XOR_MAPPED_ADDRESS:
                            cookie_int = int(codecs.encode(MAGIC_COOKIE, "hex"), 16)
                            port ^= cookie_int >> 16
                            ip = int(codecs.encode(attr_value[4:8], "hex"), 16) ^ cookie_int
                            ip = socket.inet_ntoa(long_to_bytes(ip, 4))
                            response["xor_ip"], response["xor_port"] = ip, port
                        elif attr_type == MAPPED_ADDRESS:
                            response["ext_ip"], response["ext_port"] = ip, port
                        elif attr_type == SOURCE_ADDRESS:
                            response["src_ip"], response["src_port"] = ip, port
                        elif attr_type == CHANGED_ADDRESS:
                            response["change_ip"], response["change_port"] = ip, port
                    else:  # family == 0x02:  # IPv6
                        ip = socket.inet_ntop(socket.AF_INET6, attr_value[4:20])
                        if attr_type == XOR_MAPPED_ADDRESS:
                            cookie_int = int(codecs.encode(MAGIC_COOKIE, "hex"), 16)
                            port ^= cookie_int >> 16
                            ip = int(codecs.encode(attr_value[4:20], "hex"), 16) ^ (
                                cookie_int << 96 | trans_id
                            )
                            ip = socket.inet_ntop(socket.AF_INET6, long_to_bytes(ip, 32))
                            response["xor_ip"], response["xor_port"] = ip, port
                        elif attr_type == MAPPED_ADDRESS:
                            response["ext_ip"], response["ext_port"] = ip, port
                        elif attr_type == SOURCE_ADDRESS:
                            response["src_ip"], response["src_port"] = ip, port
                        elif attr_type == CHANGED_ADDRESS:
                            response["change_ip"], response["change_port"] = ip, port
            # Prefer, when possible, to use XORed IPs and ports
            xor_ip, xor_port = response.get("xor_ip", None), response.get("xor_port", None)
            if xor_ip is not None:
                response["ext_ip"] = xor_ip
            if xor_port is not None:
                response["ext_port"] = xor_port
            break
    sock.settimeout(old_timeout)
    return response
