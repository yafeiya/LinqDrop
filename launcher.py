"""Single entry: reuse the running hub, or start it and open its control page."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.runtime'
APP_ID = 'linqdrop-lan-hub'


def request(url, body=None, admin=None):
    headers = {'Content-Type': 'application/json'}
    if admin:
        headers['X-LinqDrop-Admin'] = admin
    raw = None if body is None else json.dumps(body).encode('utf-8')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, data=raw, headers=headers), timeout=2) as response:
        return json.load(response)


def existing():
    try:
        state = json.loads((RUNTIME / 'instance.json').read_text(encoding='utf-8'))
        if Path(state['root']).resolve() != ROOT:
            return None
        url = f"http://127.0.0.1:{int(state['port'])}/"
        health = request(url + 'api/health')
        return url if health.get('app') == APP_ID and health.get('boot') == state.get('boot') else None
    except (OSError, ValueError, KeyError):
        return None


def choose_port(preferred):
    for port in range(preferred, min(65536, preferred + 20)):
        sock = socket.socket()
        try:
            sock.bind(('0.0.0.0', port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    raise RuntimeError('常用端口均被占用，请关闭旧程序后重试。')


def acquire_lock(handle):
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def main():
    parser = argparse.ArgumentParser(description='邻传 LinqDrop 统一入口')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--diagnose', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口范围必须为 1..65535')
    if sys.version_info < (3, 9):
        raise RuntimeError('需要 Python 3.9 或更新版本。')
    if not (ROOT / 'public/lib/qrcode.js').is_file():
        raise RuntimeError('文件不完整，请先完整解压离线包。')
    RUNTIME.mkdir(exist_ok=True)
    url = existing()
    if args.stop or args.diagnose:
        if not url:
            raise RuntimeError('服务尚未启动，请先双击 LinqDrop.bat。')
        config = request(url + 'api/config')
        result = request(url + ('api/host/shutdown' if args.stop else 'api/host/report'), {}, config['adminKey'])
        if args.diagnose:
            output = RUNTIME / 'network-diagnostics.txt'
            output.write_text(result['text'], encoding='utf-8')
            print(result['text'])
        return 0
    with (RUNTIME / 'launch.lock').open('a+b') as guard:
        if guard.tell() == 0:
            guard.write(b'0')
            guard.flush()
        for attempt in range(80):
            try:
                acquire_lock(guard)
                break
            except OSError:
                time.sleep(.25)
        else:
            raise RuntimeError('另一个启动过程尚未结束，请稍后重试。')
        url = existing()
        if not url:
            port = choose_port(args.port)
            environment = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}
            with (RUNTIME / 'server.log').open('ab') as log:
                process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'server.py'), '--port', str(port), '--no-browser'],
                                           cwd=ROOT, stdout=log, stderr=log, env=environment,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                           start_new_session=os.name != 'nt')
            for attempt in range(100):
                url = existing()
                if url:
                    break
                if process.poll() is not None:
                    tail = (RUNTIME / 'server.log').read_text(encoding='utf-8', errors='replace')[-1800:]
                    raise RuntimeError('启动失败。诊断日志：\n' + tail)
                time.sleep(.2)
            else:
                raise RuntimeError('服务启动时间较长，请稍后再次双击入口。')
    if not args.no_browser:
        webbrowser.open(url)
    print('LinqDrop ready: ' + url)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        RUNTIME.mkdir(exist_ok=True)
        (RUNTIME / 'launch-error.txt').write_text(str(error), encoding='utf-8')
        print(str(error), file=sys.stderr)
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), '邻传 LinqDrop 启动提示', 0x10)
        raise SystemExit(1)
