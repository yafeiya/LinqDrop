"""Network inventory and LAN address selection; Python standard library only."""
import ipaddress
import json
import os
import socket
import subprocess


def usable_ipv4(value):
    try:
        ip = ipaddress.IPv4Address(value)
        return not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified)
    except (ValueError, TypeError):
        return False


def route_ipv4():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('192.0.2.1', 80))
        value = sock.getsockname()[0]
        return value if usable_ipv4(value) else None
    except OSError:
        return None
    finally:
        sock.close()


def powershell_json(command, timeout=12):
    prefix = "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $ErrorActionPreference='Stop'; "
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', prefix + command],
                            capture_output=True, timeout=timeout, encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'PowerShell query failed')
    value = result.stdout.lstrip('\ufeff').strip()
    return json.loads(value) if value else []


def windows_interfaces():
    command = r"""@(Get-NetIPConfiguration -All | Where-Object { $_.NetAdapter.Status -eq 'Up' -and $_.IPv4Address } | ForEach-Object {
        $cfg = $_
        $profile = Get-NetConnectionProfile -InterfaceIndex $cfg.InterfaceIndex -ErrorAction SilentlyContinue | Select-Object -First 1
        [pscustomobject]@{
            name = [string]$cfg.InterfaceAlias
            description = [string]$cfg.InterfaceDescription
            index = [int]$cfg.InterfaceIndex
            hardware = [bool]$cfg.NetAdapter.HardwareInterface
            virtual = [bool]$cfg.NetAdapter.Virtual
            profile = [string]$profile.NetworkCategory
            gateway = [bool]$cfg.IPv4DefaultGateway
            addresses = @($cfg.IPv4Address | ForEach-Object { $_.IPAddress })
        }
    }) | ConvertTo-Json -Depth 5 -Compress"""
    values = powershell_json(command)
    if isinstance(values, dict):
        values = [values]
    rows = []
    for value in values:
        addresses = value.get('addresses', [])
        if isinstance(addresses, str):
            addresses = [addresses]
        for address in addresses:
            if usable_ipv4(address):
                rows.append({**value, 'ip': address})
    return rows


def collect_interfaces():
    rows = []
    if os.name == 'nt':
        try:
            rows = windows_interfaces()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
    candidates = []
    routed = route_ipv4()
    if routed:
        candidates.append(routed)
    try:
        candidates += [item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    known = {row['ip'] for row in rows}
    for address in candidates:
        if usable_ipv4(address) and address not in known:
            rows.append({'ip': address, 'name': 'Network', 'hardware': False, 'virtual': False,
                         'gateway': address == routed, 'profile': '', 'description': ''})
            known.add(address)
    return rows


def choose_primary(rows, routed=None):
    if not rows:
        return routed if usable_ipv4(routed) else '127.0.0.1'
    def score(row):
        description = (row.get('name', '') + ' ' + row.get('description', '')).lower()
        virtual_hint = any(word in description for word in ['vmware', 'virtualbox', 'vethernet', 'wsl', 'tailscale', 'zerotier', 'wireguard', 'vpn', 'tap-', 'tun'])
        points = 100 if row.get('hardware') and not row.get('virtual') else 0
        points += 30 if row.get('gateway') else 0
        points += 15 if row.get('ip') == routed else 0
        points -= 100 if row.get('virtual') or virtual_hint else 0
        return points
    return max(rows, key=score)['ip']
