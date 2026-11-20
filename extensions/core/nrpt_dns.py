"""
nrpt_dns.py -- Windows NRPT (Name Resolution Policy Table) Selective DNS Routing
Enables selective DNS routing for Google Antigravity without a system-wide VPN.
Only routes cloudcode-pa.googleapis.com, daily-cloudcode-pa.googleapis.com, and aicode.googleapis.com.
"""

from __future__ import annotations
import ipaddress
import os
import re
import socket
import sys
import json
import subprocess
from pathlib import Path

NRPT_COMMENT = "AG_PATCHER_NRPT"
_LEGACY_NRPT_COMMENT = "AG_UN" + "LOCKER_NRPT"
NRPT_NAMESPACES = [
    "cloudcode-pa.googleapis.com",
    "daily-cloudcode-pa.googleapis.com",
    "aicode.googleapis.com",
    "generativelanguage.googleapis.com",
]

CONFIG_DIR = Path.home() / ".antigravity_tools"
CONFIG_FILE = CONFIG_DIR / "dns_config.json"

# Public fallback DNS servers known for unblocking Cloud Code PA endpoints
PUBLIC_UNBLOCK_SERVERS = [
    {"ip": "37.230.192.51", "name": "Geohide Unblock DNS (Primary)"},
    {"ip": "212.109.195.93", "name": "Comss.one Unblock DNS"},
    {"ip": "1.1.1.1", "name": "Cloudflare DNS (Fallback)"}
]


def _run_ps(script: str) -> tuple[int, str]:
    """Execute PowerShell script quietly without showing windows."""
    try:
        flags = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=flags
        )
        return proc.returncode, (proc.stdout or "").strip()
    except Exception as e:
        return 1, str(e)


def _flush_dns_cache() -> None:
    """Best-effort DNS resolver cache flush on Windows after NRPT changes."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["ipconfig", "/flushdns"],
            capture_output=True,
            timeout=5,
            creationflags=0x08000000
        )
    except Exception:
        pass


def is_nrpt_supported() -> bool:
    """NRPT is supported on Windows 8, 10, 11 and Windows Server."""
    return sys.platform == "win32"


def get_dns_config() -> dict:
    if not CONFIG_FILE.exists():
        return {
            "enabled": False,
            "mode": "public",
            "server_ip": PUBLIC_UNBLOCK_SERVERS[0]["ip"],
            "server_name": PUBLIC_UNBLOCK_SERVERS[0]["name"],
            "private_key": "",
            "custom_ip": ""
        }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False, "mode": "public", "server_ip": PUBLIC_UNBLOCK_SERVERS[0]["ip"]}


def save_dns_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_preferred_dns_ip(cfg: dict | None = None) -> str:
    """Resolve the preferred DNS IP respecting active mode (public vs private)."""
    cfg = cfg if cfg is not None else get_dns_config()
    if cfg.get("mode") == "private":
        priv = str(cfg.get("custom_ip") or cfg.get("server_ip") or "").strip()
        if priv:
            return priv
    srv = str(cfg.get("server_ip") or "").strip()
    return srv or PUBLIC_UNBLOCK_SERVERS[0]["ip"]


_VALID_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def check_dns_resolver_reachable(ip: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Test if a DNS server responds to a standard DNS query on UDP port 53 within timeout."""
    # Build minimal valid DNS query for google.com (A record, class IN)
    header = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    qname = b"\x06google\x03com\x00"
    qtype_class = b"\x00\x01\x00\x01"
    packet = header + qname + qtype_class

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (ip, 53))
        data, _ = sock.recvfrom(512)
        if len(data) >= 12:
            return True, "Reachable"
        return False, "Malformed DNS response"
    except socket.timeout:
        return False, f"timed out after {timeout:.1f}s"
    except Exception as e:
        return False, str(e)
    finally:
        sock.close()


def check_dns_substitution(
    resolver_ip: str,
    domain: str = "daily-cloudcode-pa.googleapis.com",
    ref_ip: str = "8.8.8.8",
    timeout: float = 2.0
) -> tuple[bool, str]:
    """
    Check if a DNS resolver actively substitutes the domain with an unblock proxy IP
    rather than passing through genuine Google IPs.
    """
    try:
        from extensions.core.hosts_pin import resolve_domain_udp
    except ImportError:
        try:
            from patcher.hosts_pin import resolve_domain_udp
        except ImportError:
            return True, "Substitution verification skipped (hosts_pin unavailable)"

    ref_ips = set(resolve_domain_udp(ref_ip, domain, timeout=timeout))
    res_ips = set(resolve_domain_udp(resolver_ip, domain, timeout=timeout))
    if not res_ips:
        return False, "No response or empty answer"
    res_ips = {ip for ip in res_ips if not ip.startswith(("127.", "0.", "255."))}
    if not res_ips:
        return False, "Resolver returned loopback/bogon addresses"
    if res_ips != ref_ips:
        return True, f"Substituted: {', '.join(sorted(res_ips))}"
    return False, f"Passthrough (same as reference {ref_ip}): {', '.join(sorted(res_ips))}"


def _resolve_dns_target_ip(raw_target: str) -> tuple[str | None, str | None]:
    """Validate an IPv4/IPv6 DNS server address or resolve a hostname to IPv4."""
    val = str(raw_target or "").strip()
    if not val:
        return None, "DNS server address is empty."
    try:
        ip_obj = ipaddress.ip_address(val)
        return str(ip_obj), None
    except ValueError:
        pass
    if not _VALID_HOST_RE.match(val):
        return None, f"Invalid DNS IP or hostname: {val!r}"
    try:
        infos = socket.getaddrinfo(val, None, socket.AF_INET, socket.SOCK_DGRAM)
        for info_row in infos:
            sockaddr = info_row[4]
            if sockaddr and sockaddr[0]:
                ipaddress.ip_address(sockaddr[0])
                return str(sockaddr[0]), None
    except OSError as ex:
        return None, f"Could not resolve DNS server host '{val}': {ex}"
    return None, f"No IPv4 address resolved for DNS server '{val}'."


def _remove_existing_nrpt_rules() -> tuple[int, str]:
    ps_remove = f"""
    Get-DnsClientNrptRule -ErrorAction SilentlyContinue | Where-Object {{ $_.Comment -like '*{NRPT_COMMENT}*' -or $_.Comment -like '*{_LEGACY_NRPT_COMMENT}*' }} | ForEach-Object {{
        Remove-DnsClientNrptRule -Name $_.Name -Force -ErrorAction Stop
    }}
    """
    return _run_ps(ps_remove)


def _normalize_nrpt_servers(raw_val) -> str:
    if not raw_val:
        return ""
    if isinstance(raw_val, (list, tuple)):
        parts = [str(x).strip() for x in raw_val if str(x).strip()]
        return ",".join(parts)
    parts = [p.strip() for p in re.split(r"[;,\s]+", str(raw_val)) if p.strip()]
    return ",".join(parts)


def _get_nrpt_status_winreg() -> dict | None:
    """Fast <1ms Windows NRPT lookup directly via winreg (no PowerShell spawn)."""
    import winreg
    reg_roots = (
        r"SYSTEM\CurrentControlSet\Services\Dnscache\Parameters\DnsPolicyConfig",
        r"SOFTWARE\Policies\Microsoft\Windows NT\DNSClient\DnsPolicyConfig",
    )
    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    for root_path in reg_roots:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path, 0, access) as root_key:
                subkey_count = winreg.QueryInfoKey(root_key)[0]
                for idx in range(subkey_count):
                    sub_name = winreg.EnumKey(root_key, idx)
                    try:
                        with winreg.OpenKey(root_key, sub_name, 0, access) as sk:
                            comment = ""
                            servers_raw = ""
                            try:
                                comment, _ = winreg.QueryValueEx(sk, "Comment")
                            except OSError:
                                pass
                            if NRPT_COMMENT not in str(comment) and _LEGACY_NRPT_COMMENT not in str(comment):
                                continue
                            try:
                                servers_raw, _ = winreg.QueryValueEx(sk, "GenericDNSServers")
                            except OSError:
                                pass
                            return {
                                "supported": True,
                                "active": True,
                                "rule_id": sub_name,
                                "server": _normalize_nrpt_servers(servers_raw),
                            }
                    except OSError:
                        continue
        except FileNotFoundError:
            continue
    return {"supported": True, "active": False, "rule_id": "", "server": ""}


def get_nrpt_status() -> dict:
    """Query active Windows NRPT rules for Antigravity namespaces."""
    if not is_nrpt_supported():
        return {"supported": False, "active": False, "rule_id": "", "server": ""}

    try:
        reg_status = _get_nrpt_status_winreg()
        if reg_status is not None:
            return reg_status
    except Exception:
        pass

    ps_check = f"""
    $rules = Get-DnsClientNrptRule -ErrorAction SilentlyContinue | Where-Object {{ $_.Comment -like '*{NRPT_COMMENT}*' -or $_.Comment -like '*{_LEGACY_NRPT_COMMENT}*' }}
    if ($rules) {{
        $r = $rules | Select-Object -First 1
        $ns = ($r.NameServers -join ',')
        [PSCustomObject]@{{
            Active = $true
            RuleId = $r.Name
            Server = $ns
        }} | ConvertTo-Json -Compress
    }} else {{
        [PSCustomObject]@{{
            Active = $false
            RuleId = ''
            Server = ''
        }} | ConvertTo-Json -Compress
    }}
    """
    code, out = _run_ps(ps_check)
    if code == 0 and out:
        try:
            data = json.loads(out)
            return {
                "supported": True,
                "active": bool(data.get("Active")),
                "rule_id": data.get("RuleId", ""),
                "server": data.get("Server", "")
            }
        except Exception:
            pass

    return {"supported": True, "active": False, "rule_id": "", "server": ""}


def enable_nrpt_rule(
    dns_ip: str,
    mode: str | None = None,
    server_name: str | None = None,
    check_live: bool = True
) -> tuple[bool, str]:
    """Register selective NRPT rules for Antigravity hostnames on Windows."""
    if not is_nrpt_supported():
        return False, "NRPT is only supported on Windows 10/11."

    resolved_ip, err_msg = _resolve_dns_target_ip(dns_ip)
    raw_target = str(dns_ip or "").strip()
    is_private_req = (mode == "private") or (
        mode is None and bool(raw_target) and get_dns_config().get("mode") == "private"
    )

    if not resolved_ip:
        if not is_private_req:
            resolved_ip = PUBLIC_UNBLOCK_SERVERS[0]["ip"]
        else:
            return False, err_msg or "Invalid DNS IP."

    if check_live:
        orig_ip = resolved_ip
        is_live, probe_err = check_dns_resolver_reachable(resolved_ip, timeout=2.0)
        if not is_live and not is_private_req:
            # Automatically fail over across known reachable public unblock servers,
            # preferring resolvers that actively substitute Cloud Code endpoints.
            first_reachable = None
            for srv in PUBLIC_UNBLOCK_SERVERS:
                cand_ip = srv["ip"]
                if cand_ip == resolved_ip:
                    continue
                ok_cand, _ = check_dns_resolver_reachable(cand_ip, timeout=2.0)
                if not ok_cand:
                    continue
                if first_reachable is None:
                    first_reachable = (cand_ip, srv["name"])
                ok_sub, _ = check_dns_substitution(cand_ip, timeout=1.5)
                if ok_sub:
                    resolved_ip = cand_ip
                    raw_target = cand_ip
                    server_name = srv["name"]
                    is_live = True
                    break
            if not is_live and first_reachable is not None:
                resolved_ip, server_name = first_reachable
                raw_target = resolved_ip
                is_live = True
        if not is_live:
            return False, f"DNS server {orig_ip} is unreachable ({probe_err}). NRPT rule was NOT applied to protect your network."

    public_match = next((s for s in PUBLIC_UNBLOCK_SERVERS if s["ip"] == resolved_ip), None)
    effective_mode = mode or ("private" if is_private_req else ("public" if public_match else "private"))

    # 1. Clean existing rules first
    _remove_existing_nrpt_rules()

    # 2. Add rule for all target namespaces
    ns_quoted = ", ".join(f'"{ns}"' for ns in NRPT_NAMESPACES)
    ps_add = f"""
    Add-DnsClientNrptRule -Namespace @({ns_quoted}) -Nameservers @("{resolved_ip}") -Comment "{NRPT_COMMENT}" -ErrorAction Stop
    """
    code, out = _run_ps(ps_add)
    if code == 0:
        _flush_dns_cache()
        cfg = get_dns_config()
        cfg["enabled"] = True
        cfg["mode"] = effective_mode
        cfg["server_ip"] = resolved_ip
        if server_name:
            cfg["server_name"] = server_name
        elif public_match:
            cfg["server_name"] = public_match["name"]
        else:
            cfg["server_name"] = raw_target
        if effective_mode == "private":
            cfg["custom_ip"] = raw_target
        save_dns_config(cfg)
        suffix = f" ({raw_target} -> {resolved_ip})" if raw_target != resolved_ip else f" ({resolved_ip})"
        return True, f"Selective DNS routing activated via{suffix}"
    else:
        return False, f"PowerShell NRPT Error (requires Administrator rights): {out}"


def disable_nrpt_rule() -> tuple[bool, str]:
    """Remove selective NRPT rules created by Antigravity Patcher."""
    if not is_nrpt_supported():
        return True, "NRPT not supported."

    code, out = _remove_existing_nrpt_rules()
    _flush_dns_cache()
    remaining = get_nrpt_status()
    if remaining.get("active"):
        err_detail = out or "Administrator rights required"
        return False, f"Could not remove NRPT rules: {err_detail}"

    cfg = get_dns_config()
    cfg["enabled"] = False
    save_dns_config(cfg)
    return True, "NRPT rules removed."


if __name__ == "__main__":
    if "--fix-400" in sys.argv:
        target_ip = get_preferred_dns_ip()
        ok_flag, msg = enable_nrpt_rule(target_ip)
        print(msg)
        sys.exit(0 if ok_flag else 1)
