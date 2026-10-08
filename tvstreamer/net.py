"""Local addresses and small HTTP helpers."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request


def ipv4_addresses() -> list[str]:
    found: list[str] = []

    def add(ip: str) -> None:
        if not ip or ip in found:
            return
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return
        if address.is_loopback or address.is_link_local or address.is_multicast:
            return
        found.append(ip)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    private = [ip for ip in found if is_private(ip)]
    return private or found


def is_private(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address.version == 4 and address.is_private


def is_lan_client(ip: str) -> bool:
    """Loopback for local checks, otherwise a home-network address."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version != 4 or address.is_link_local or address.is_multicast or address.is_unspecified:
        return False
    return address.is_loopback or address.is_private


def is_http_url(url: str) -> bool:
    from urllib.parse import urlsplit
    if any(ord(ch) < 32 for ch in url):
        return False
    parts = urlsplit(url.strip())
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def http_post(url: str, body: str, headers: dict[str, str], timeout: float = 5) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
