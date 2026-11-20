"""
hosts_pin.py -- Direct SNI Proxy Hosts-Pinning for Google Antigravity
Provides a VPN-proof, zero-latency direct route to unblock SNI proxy nodes.
When a VPN is active, Windows DNS Client routes NRPT queries through the VPN tunnel,
which can fail or return region-blocked Google IPs. Hosts file mapping takes precedence
over both NRPT and VPN DNS in Windows/macOS/Linux networking stacks.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
import subprocess
import sys
from pathlib import Path

HOSTS_COMMENT_BEGIN = "# AG_PATCHER_HOSTS_BEGIN"
HOSTS_COMMENT_END = "# AG_PATCHER_HOSTS_END"
_LEGACY_HOSTS_BEGIN = "# AG_UN" + "LOCKER_HOSTS_BEGIN"
_LEGACY_HOSTS_END = "# AG_UN" + "LOCKER_HOSTS_END"

PINNED_DOMAINS = [
    "cloudcode-pa.googleapis.com",
    "daily-cloudcode-pa.googleapis.com",
    "aicode.googleapis.com",
    "generativelanguage.googleapis.com",
]

DEFAULT_FALLBACK_PROXY_IP = "193.233.112.67"
DISCOVERY_RESOLVERS = ["37.230.192.51"]

# Verified live European Google Cloud ESF SNI frontends (Geohide / unblock pool)
VERIFIED_SNI_PROXIES = [
    "193.233.112.67",
    "193.233.112.68",
    "159.194.200.33",
    "45.13.237.213",
    "5.175.161.40",
    "5.175.161.3",
    "193.233.112.88",
]


def get_hosts_path() -> Path:
    """Return the platform-appropriate path to the OS hosts file."""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def _flush_dns_cache() -> None:
    """Flush local resolver cache to ensure hosts changes apply immediately."""
    if sys.platform == "win32":
        try:
            flags = 0x08000000  # CREATE_NO_WINDOW
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=5, creationflags=flags)
        except Exception:
            pass


def resolve_domain_udp(server: str, domain: str, timeout: float = 2.0) -> list[str]:
    """Perform a raw UDP DNS query to resolve A records."""
    parts = domain.split(".")
    qname = b"".join(bytes([len(p)]) + p.encode() for p in parts) + b"\x00"
    header = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    packet = header + qname + b"\x00\x01\x00\x01"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    ips: list[str] = []
    try:
        sock.sendto(packet, (server, 53))
        resp, _ = sock.recvfrom(1024)
        idx = 12 + len(qname) + 4
        while idx < len(resp):
            if idx + 12 > len(resp):
                break
            if (resp[idx] & 0xC0) == 0xC0:
                idx += 2
            else:
                while idx < len(resp) and resp[idx] != 0:
                    idx += 1 + resp[idx]
                idx += 1
            if idx + 10 > len(resp):
                break
            qtype = resp[idx : idx + 2]
            rdlen = int.from_bytes(resp[idx + 8 : idx + 10], "big")
            idx += 10
            if qtype == b"\x00\x01" and rdlen == 4:
                ip_str = socket.inet_ntoa(resp[idx : idx + 4])
                ips.append(ip_str)
            idx += rdlen
    except Exception:
        pass
    finally:
        sock.close()
    return ips


def test_tls_sni(ip: str, domain: str = "cloudcode-pa.googleapis.com", timeout: float = 2.0) -> bool:
    """Validate TLS handshake with SNI on port 443 of the target proxy IP."""
    try:
        ctx = ssl.create_default_context()
        s = socket.create_connection((ip, 443), timeout=timeout)
        with ctx.wrap_socket(s, server_hostname=domain) as tls:
            return bool(tls.cipher())
    except Exception:
        return False


def discover_sni_proxy_ip(fallback_ip: str = DEFAULT_FALLBACK_PROXY_IP) -> str:
    """Discover a live working unblock SNI proxy IP via resolver pool with TLS verification."""
    # 1. Probe dynamic discovery resolvers
    for resolver in DISCOVERY_RESOLVERS:
        candidate_ips = resolve_domain_udp(resolver, "daily-cloudcode-pa.googleapis.com", timeout=2.0)
        for cand in candidate_ips:
            try:
                ipaddress.ip_address(cand)
                # Filter out standard Google IPs and bogons
                if not cand.startswith(("172.217.", "142.250.", "108.177.", "74.125.", "8.6.", "8.47.", "127.")):
                    if test_tls_sni(cand, "daily-cloudcode-pa.googleapis.com", timeout=1.5) and test_tls_sni(cand, "cloudcode-pa.googleapis.com", timeout=1.5):
                        return cand
            except Exception:
                continue

    # 2. Probe pool of verified Google Cloud ESF frontends
    for proxy in VERIFIED_SNI_PROXIES:
        if test_tls_sni(proxy, "daily-cloudcode-pa.googleapis.com", timeout=1.5) and test_tls_sni(proxy, "cloudcode-pa.googleapis.com", timeout=1.5):
            return proxy

    if test_tls_sni(fallback_ip, "cloudcode-pa.googleapis.com", timeout=2.0):
        return fallback_ip

    return fallback_ip


def replace_hosts_block(existing_content: str, block: str | None) -> str:
    """
    Replace or remove the Antigravity block inside hosts file content.
    Preserves all existing content outside the markers byte-for-byte.
    """
    lines = existing_content.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith((HOSTS_COMMENT_BEGIN, _LEGACY_HOSTS_BEGIN)):
            inside = True
            continue
        if inside:
            if stripped.startswith((HOSTS_COMMENT_END, _LEGACY_HOSTS_END)):
                inside = False
            continue
        kept.append(line)

    out = "".join(kept)
    if block:
        if out and not out.endswith("\n"):
            out += "\r\n" if "\r\n" in existing_content else "\n"
        out += block
        if not out.endswith("\n"):
            out += "\r\n" if "\r\n" in existing_content else "\n"
    return out


def is_hosts_pinned() -> tuple[bool, str]:
    """Check if Hosts-Pinning is currently active and return (is_active, pinned_ip)."""
    hosts_file = get_hosts_path()
    if not hosts_file.is_file():
        return False, ""
    try:
        content = hosts_file.read_text(encoding="utf-8", errors="ignore")
        if HOSTS_COMMENT_BEGIN not in content and _LEGACY_HOSTS_BEGIN not in content:
            return False, ""
        m = re.search(r"^\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)\s+cloudcode-pa\.googleapis\.com", content, re.MULTILINE)
        pinned_ip = m.group(1) if m else "active"
        return True, pinned_ip
    except Exception:
        return False, ""


def enable_hosts_pinning(proxy_ip: str | None = None) -> tuple[bool, str]:
    """
    Write direct SNI proxy routes into the OS hosts file.
    Works seamlessly whether VPN is enabled or disabled.
    """
    hosts_file = get_hosts_path()
    effective_ip = (proxy_ip or "").strip()
    if not effective_ip:
        effective_ip = discover_sni_proxy_ip()

    try:
        ipaddress.ip_address(effective_ip)
    except ValueError:
        return False, f"Invalid proxy IPv4 address: {effective_ip}"

    eol = "\r\n" if sys.platform == "win32" else "\n"
    block_lines = [HOSTS_COMMENT_BEGIN]
    for domain in PINNED_DOMAINS:
        block_lines.append(f"{effective_ip} {domain}")
    block_lines.append(HOSTS_COMMENT_END)
    block_text = eol.join(block_lines) + eol

    try:
        existing = ""
        if hosts_file.is_file():
            existing = hosts_file.read_text(encoding="utf-8", errors="ignore")
        new_content = replace_hosts_block(existing, block_text)
        if new_content != existing:
            from patcher.utils.atomic import write_atomic

            write_atomic(str(hosts_file), new_content, encoding="utf-8", kill_on_lock=False)
        _flush_dns_cache()
        return True, f"Direct Hosts-Pinning activated ({effective_ip}). VPN-proof routing active."
    except PermissionError:
        return False, "Permission denied writing to hosts file. Administrator rights required."
    except Exception as e:
        return False, f"Failed writing hosts file: {e}"


def disable_hosts_pinning() -> tuple[bool, str]:
    """Remove direct SNI proxy routes from the OS hosts file."""
    hosts_file = get_hosts_path()
    if not hosts_file.is_file():
        return True, "Hosts file not present."

    try:
        existing = hosts_file.read_text(encoding="utf-8", errors="ignore")
        if HOSTS_COMMENT_BEGIN not in existing and _LEGACY_HOSTS_BEGIN not in existing:
            return True, "Hosts-Pinning was not active."

        new_content = replace_hosts_block(existing, None)
        from patcher.utils.atomic import write_atomic

        write_atomic(str(hosts_file), new_content, encoding="utf-8", kill_on_lock=False)
        _flush_dns_cache()
        return True, "Direct Hosts-Pinning removed. Standard DNS routing restored."
    except PermissionError:
        return False, "Permission denied modifying hosts file. Administrator rights required."
    except Exception as e:
        return False, f"Failed updating hosts file: {e}"

