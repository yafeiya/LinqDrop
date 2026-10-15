"""Local diagnostics and a user-invoked, scoped Windows firewall repair."""
import argparse
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from network_info import collect_interfaces, choose_primary, route_ipv4, powershell_json

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'


def direct_json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        return json.load(response)


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def windows_status(port, address):
    if os.name != 'nt':
        return {'firewall': 'manual', 'message': '请允许系统防火墙中的局域网入站连接。'}
    program = sys.executable.replace("'", "''")
    command = f"""$allowed = $false; $blocks = @();
    $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -DisplayName 'LinqDrop Scoped LAN TCP {port}' -ErrorAction SilentlyContinue);
    foreach ($rule in $rules) {{
      $app = $rule | Get-NetFirewallApplicationFilter;
      $ports = $rule | Get-NetFirewallPortFilter;
      $ips = $rule | Get-NetFirewallAddressFilter;
      if ($rule.Enabled -eq 'True' -and $rule.Action -eq 'Allow' -and $rule.Direction -eq 'Inbound' -and [string]$rule.Profile -eq 'Any' -and
          $app.Program -ieq '{program}' -and $ports.LocalPort -contains '{port}' -and $ips.LocalAddress -contains '{address}' -and $ips.RemoteAddress -contains 'LocalSubnet') {{ $allowed = $true }}
    }}
    $filters = @(Get-NetFirewallApplicationFilter -PolicyStore ActiveStore -Program '{program}' -ErrorAction SilentlyContinue);
    if ($filters.Count) {{ $blocks = @($filters | Get-NetFirewallRule | Where-Object {{ $_.Enabled -eq 'True' -and $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' }}) }}
    [pscustomobject]@{{ allowed=$allowed; blocked=($blocks.Count -gt 0) }} | ConvertTo-Json -Compress"""
    result = powershell_json(command)
    if result.get('blocked'):
        return {'firewall': 'blocked', 'message': '存在 Python 入站阻止规则，需要在 Windows 防火墙中检查。'}
    if result.get('allowed'):
        return {'firewall': 'allowed', 'message': '已检测到适用于当前地址与端口的局域网放行规则。'}
    return {'firewall': 'missing', 'message': '尚未检测到匹配规则。如手机无法打开，请点击“修复手机访问”。'}


def monitor(hub):
    while not hub.stopping.is_set():
        try:
            interfaces = collect_interfaces()
            address = choose_primary(interfaces, route_ipv4())
            with hub.lock:
                hub.interfaces = interfaces
                hub.address = address
            status = windows_status(hub.port, address) if not ipaddress.ip_address(address).is_loopback else {'firewall': 'manual', 'message': '未检测到局域网地址，请先连接 Wi-Fi、以太网或手机热点。'}
            status.update({'checking': False, 'address': address, 'updated': time.time()})
            result_path = RUNTIME / 'firewall-result.json'
            if result_path.is_file():
                status['repairResult'] = json.loads(result_path.read_text(encoding='utf-8'))
            hub.host_status = status
            state_path = RUNTIME / 'instance.json'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if state.get('boot') == hub.boot:
                state['ip'] = address
                atomic_json(state_path, state)
        except Exception as error:
            hub.host_status = {'checking': False, 'firewall': 'unknown', 'message': '网络检测暂不可用，可导出诊断查看。', 'detail': type(error).__name__}
        hub.cleanup()
        hub.stopping.wait(12)


def request_firewall_repair(port, address):
    if os.name != 'nt':
        return {'ok': False, 'message': f'请在系统防火墙中允许局域网 TCP {port} 入站连接。'}
    parameters = subprocess.list2cmdline([str(ROOT / 'system_tools.py'), '--repair', '--port', str(port), '--ip', address])
    result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, parameters, str(ROOT), 0)
    if result <= 32:
        return {'ok': False, 'message': '系统未授予管理员权限。可再次点击修复并在系统提示中确认。'}
    return {'ok': True, 'message': '已打开 Windows 权限确认。完成后页面会自动更新检测结果。'}


def repair(port, address):
    if os.name != 'nt' or not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('需要 Windows 管理员权限')
    state = json.loads((RUNTIME / 'instance.json').read_text(encoding='utf-8'))
    if state.get('port') != port or Path(state.get('root', '')).resolve() != ROOT:
        raise RuntimeError('启动状态已变化，请重新打开主机页面')
    if address not in {row['ip'] for row in collect_interfaces()}:
        raise RuntimeError('所选地址不属于当前活动网卡')
    if direct_json(f'http://127.0.0.1:{port}/api/health').get('boot') != state.get('boot'):
        raise RuntimeError('未找到匹配的运行服务')
    pid, program = int(state['pid']), str(state['python'])
    info = powershell_json(f"[pscustomobject]@{{ listeners=@(Get-NetTCPConnection -State Listen -LocalPort {port} | Select-Object OwningProcess); process=(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | Select-Object ExecutablePath) }} | ConvertTo-Json -Depth 4 -Compress")
    if pid not in {row['OwningProcess'] for row in info['listeners']} or Path(info['process']['ExecutablePath']).resolve() != Path(program).resolve():
        raise RuntimeError('无法核实服务进程，未修改防火墙')
    args = ['netsh.exe', 'advfirewall', 'firewall', 'add', 'rule', f'name=LinqDrop Scoped LAN TCP {port}',
            'dir=in', 'action=allow', 'enable=yes', 'protocol=TCP', f'localport={port}', f'localip={address}',
            'remoteip=localsubnet', 'profile=any', f'program={program}', 'edge=no']
    # Replace only the rule owned by this tool; never disable other Block rules.
    subprocess.run(['netsh.exe', 'advfirewall', 'firewall', 'delete', 'rule', f'name=LinqDrop Scoped LAN TCP {port}'], capture_output=True)
    result = subprocess.run(args, capture_output=True)
    if result.returncode:
        raise RuntimeError('Windows 未能添加放行规则，请导出诊断检查组织策略')
    return {'ok': True, 'message': f'已放行局域网设备访问 {address}:{port}；程序和端口均已限定。', 'time': time.time()}


def diagnostic_report(hub):
    lines = ['LinqDrop 局域网诊断', '版本：2.0.1', '时间：' + time.strftime('%Y-%m-%d %H:%M:%S'),
             '运行时长：' + str(int(time.time() - hub.started)) + ' 秒', '端口：' + str(hub.port),
             'Python：' + sys.executable, '主机：' + hub.url(), '', '活动网卡：']
    for row in hub.interfaces:
        lines.append(f"  {row['name']} | {row['ip']} | {row.get('profile', '')} | virtual={row.get('virtual', False)}")
    lines += ['', '本机 HTTP 检测（绕过代理）：']
    for address in dict.fromkeys(['127.0.0.1', hub.address]):
        try:
            value = direct_json(hub.url(address) + 'api/health')
            lines.append(f'  {address}:{hub.port} | ' + ('PASS' if value.get('boot') == hub.boot else '服务不匹配'))
        except Exception as error:
            lines.append(f'  {address}:{hub.port} | FAIL | {type(error).__name__}')
    lines += ['', '防火墙与网络状态：', json.dumps(hub.host_status, ensure_ascii=False, indent=2), '',
              '若手机仍超时：确认相同 Wi-Fi/热点、代理对局域网直连、路由器未开启访客隔离/AP 隔离。',
              '本报告只验证主机侧；页面能打开后，再验证设备加入和实际文件下载。',
              '报告不包含访问码、设备令牌、下载凭据或文件名。']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--ip', required=True)
    args = parser.parse_args()
    try:
        outcome = repair(args.port, args.ip)
    except Exception as error:
        outcome = {'ok': False, 'message': str(error), 'time': time.time()}
    RUNTIME.mkdir(exist_ok=True)
    atomic_json(RUNTIME / 'firewall-result.json', outcome)
