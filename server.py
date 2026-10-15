"""LinqDrop 2: one local hub, multiple browser devices, no dependencies."""
import argparse
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from network_info import collect_interfaces, choose_primary, route_ipv4

ROOT = Path(__file__).resolve().parent
VERSION = '2.0.1'
APP_ID = 'linqdrop-lan-hub'
LIMIT = 2 * 1024 ** 3
TOTAL_LIMIT = 10 * 1024 ** 3
TERMINAL = {'rejected', 'cancelled', 'expired'}


def now():
    return time.time()


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Hub:
    def __init__(self, port, folder, address=None):
        self.port, self.folder = port, Path(folder)
        self.lock = threading.RLock()
        self.devices, self.tokens, self.files, self.attempts = {}, {}, {}, {}
        self.pin = str(secrets.randbelow(90000000) + 10000000)
        self.settings_path = ROOT / '.runtime' / 'settings.json'
        try:
            settings = json.loads(self.settings_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = {}
        self.require_access_code = isinstance(settings, dict) and settings.get('requireAccessCode') is True
        self.admin = secrets.token_urlsafe(32)
        self.boot = secrets.token_hex(12)
        self.started = now()
        self.stopping = threading.Event()
        self.interfaces = collect_interfaces()
        self.address = address or choose_primary(self.interfaces, route_ipv4())
        self.host_status = {'checking': True, 'firewall': 'checking'}
        self.connections = set()

    def addresses(self):
        return {r['ip'] for r in self.interfaces} | {self.address, '127.0.0.1'}

    def url(self, address=None):
        return f'http://{address or self.address}:{self.port}/'

    def set_access_requirement(self, required):
        if type(required) is not bool:
            raise ApiError('访问码开关必须为开启或关闭')
        with self.lock:
            self.settings_path.parent.mkdir(exist_ok=True)
            temporary = self.settings_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'requireAccessCode': required}), encoding='utf-8')
            temporary.replace(self.settings_path)
            if required and not self.require_access_code:
                self.pin = str(secrets.randbelow(90000000) + 10000000)
            self.require_access_code = required
            self.attempts.clear()
            return {'requireAccessCode': required, 'accessCode': self.pin if required else None}

    def auth(self, token):
        with self.lock:
            device = self.devices.get(self.tokens.get(token))
            if not device:
                raise ApiError('访问已过期，请重新加入', 401)
            device['seen'] = now()
            return device

    def visible(self, item):
        keys = ('id', 'sender', 'receiver', 'senderName', 'receiverName', 'name',
                'size', 'bytes', 'status', 'created', 'updated', 'sha256', 'downloads')
        return {k: item.get(k) for k in keys}

    def snapshot(self, device):
        with self.lock:
            peers = [{'id': d['id'], 'name': d['name'], 'kind': d['kind'], 'ip': d['ip'],
                      'online': True}
                     for d in self.devices.values() if d['id'] != device['id'] and now() - d['seen'] < 25]
            files = [self.visible(f) for f in self.files.values()
                     if device['id'] in (f['sender'], f['receiver'])]
            return {'boot': self.boot, 'me': {'id': device['id'], 'name': device['name'], 'kind': device['kind']},
                    'devices': peers, 'transfers': sorted(files, key=lambda f: f['created'], reverse=True)}

    def get_file(self, file_id, device):
        with self.lock:
            item = self.files.get(file_id)
            if not item or device['id'] not in (item['sender'], item['receiver']):
                raise ApiError('文件任务不存在或已清理', 404)
            return item

    def release(self, item, status):
        with item['lock']:
            with self.lock:
                item['status'], item['updated'] = status, now()
                item['ticket'] = None
            try:
                item['path'].unlink(missing_ok=True)
            except OSError:
                pass  # An active Windows download may still hold this file.

    def cleanup(self):
        with self.lock:
            stale = [f for f in self.files.values() if now() - f['updated'] > 1800 or f['status'] in TERMINAL]
        for item in stale:
            # Never wait for a slow upload while doing periodic cleanup.
            if item['lock'].acquire(blocking=False):
                try:
                    if now() - item['updated'] > 1800 or item['status'] in TERMINAL:
                        self.release(item, item['status'] if item['status'] in TERMINAL else 'expired')
                finally:
                    item['lock'].release()
        with self.lock:
            for key, item in list(self.files.items()):
                if item['status'] in TERMINAL and now() - item['created'] > 3600 and not item['path'].exists():
                    del self.files[key]
            for key, device in list(self.devices.items()):
                if now() - device['seen'] > 14400:
                    self.tokens.pop(device['token'], None)
                    del self.devices[key]
            self.attempts = {k: v for k, v in self.attempts.items() if now() - v[-1] < 60}


class Handler(BaseHTTPRequestHandler):
    server_version = 'LinqDrop/2.0'

    @property
    def hub(self):
        return self.server.hub

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        with self.hub.lock:
            self.hub.connections.add(self.connection)

    def finish(self):
        try:
            super().finish()
        except ConnectionError:
            pass
        finally:
            with self.hub.lock:
                self.hub.connections.discard(self.connection)

    def log_message(self, *args):
        pass  # URLs can contain one-file download capabilities.

    def output(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        try:
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(raw)
        except ConnectionError:
            pass

    def token(self):
        return self.headers.get('Authorization', '').removeprefix('Bearer ')

    def is_owner(self):
        return (ipaddress.ip_address(self.client_address[0]).is_loopback and
                secrets.compare_digest(self.headers.get('X-LinqDrop-Admin', ''), self.hub.admin))

    def require_owner(self):
        if not self.is_owner():
            raise ApiError('此操作只能在主机电脑的控制台执行', 403)

    def body(self, maximum=65536, binary=False):
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ApiError('请求长度不合法')
        if size < 0 or size > maximum:
            raise ApiError('请求过大', 413)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ApiError('请求未接收完整，请重试')
        if binary:
            return raw
        try:
            data = json.loads(raw or b'{}')
        except (ValueError, UnicodeError):
            raise ApiError('请求格式有误')
        if not isinstance(data, dict):
            raise ApiError('请求格式有误')
        return data

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def dispatch(self):
        try:
            host = self.headers.get('Host', '')
            if urlsplit('http://' + host).hostname not in self.hub.addresses() | {'localhost'}:
                raise ApiError('请使用主机电脑的局域网 IP 访问', 403)
            if self.command == 'POST':
                origin = self.headers.get('Origin')
                if origin and origin != 'http://' + host:
                    raise ApiError('请求来源不匹配', 403)
            path = urlsplit(self.path).path
            query = parse_qs(urlsplit(self.path).query)
            if path == '/api/health':
                return self.output({'app': APP_ID, 'version': VERSION, 'boot': self.hub.boot})
            if path == '/api/config' and self.command == 'GET':
                return self.config(host)
            if path.startswith('/download/') and self.command in ('GET', 'HEAD'):
                return self.download(path.rsplit('/', 1)[-1])
            if not path.startswith('/api/') and self.command in ('GET', 'HEAD'):
                return self.static(path)
            if path.startswith('/api/host/'):
                return self.host_action(path)
            if path == '/api/connect' and self.command == 'POST':
                return self.connect(self.body())
            device = self.hub.auth(self.token())
            if path == '/api/sync' and self.command == 'GET':
                return self.output(self.hub.snapshot(device))
            if path == '/api/device' and self.command == 'POST':
                data = self.body()
                with self.hub.lock:
                    device['name'] = self.clean_name(data.get('name'))
                return self.output({'ok': True})
            if path == '/api/leave' and self.command == 'POST':
                self.body()
                with self.hub.lock:
                    self.hub.tokens.pop(device['token'], None)
                    device['seen'] = 0
                return self.output({'ok': True})
            if path == '/api/transfers' and self.command == 'POST':
                return self.create_transfer(device, self.body())
            match = re.fullmatch(r'/api/transfers/([a-f0-9]{32})(?:/(chunk|action|download))?', path)
            if match:
                item = self.hub.get_file(match[1], device)
                action = match[2]
                if action is None and self.command == 'GET':
                    return self.output(self.hub.visible(item))
                if action == 'chunk' and self.command == 'POST':
                    return self.chunk(item, device, query)
                if action == 'action' and self.command == 'POST':
                    return self.file_action(item, device, self.body())
                if action == 'download' and self.command == 'POST':
                    self.body()
                    if device['id'] != item['receiver'] or item['status'] != 'ready':
                        raise ApiError('文件尚未就绪或不是接收方', 409)
                    with self.hub.lock:
                        item['ticket'] = item['ticket'] or secrets.token_urlsafe(32)
                        item['ticketIp'] = self.client_address[0]
                    return self.output({'url': '/download/' + item['ticket']})
            raise ApiError('接口不存在', 404)
        except ApiError as error:
            self.output({'error': str(error)}, error.status)
        except (ConnectionError, socket.timeout):
            pass
        except (ValueError, TypeError, KeyError, AttributeError):
            self.output({'error': '请求参数有误'}, 400)
        except OSError:
            self.output({'error': '电脑读写失败，请检查磁盘空间并重试'}, 500)

    @staticmethod
    def clean_name(value, limit=40):
        return re.sub(r'[\x00-\x1f\x7f]', '', str(value or '未命名设备')).strip()[:limit] or '未命名设备'

    def config(self, host):
        loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        with self.hub.lock:
            joined = self.token() in self.hub.tokens
            requested = urlsplit('http://' + host).hostname
            address = requested if requested in self.hub.addresses() and requested != '127.0.0.1' else self.hub.address
            value = {'app': APP_ID, 'version': VERSION, 'mode': 'local', 'boot': self.hub.boot,
                     'lanUrl': self.hub.url(address), 'port': self.hub.port, 'isHost': loopback,
                     'networkReady': not ipaddress.ip_address(self.hub.address).is_loopback,
                     'addresses': [{'ip': r['ip'], 'name': r['name'], 'virtual': r.get('virtual', False),
                                    'url': self.hub.url(r['ip'])} for r in self.hub.interfaces],
                     'maxFileSize': LIMIT, 'totalLimit': TOTAL_LIMIT,
                     'requireAccessCode': self.hub.require_access_code}
            if loopback:
                value['adminKey'] = self.hub.admin
            if self.hub.require_access_code and (loopback or joined):
                value['accessCode'] = self.hub.pin
        self.output(value)

    def connect(self, data):
        with self.hub.lock:
            existing = self.hub.devices.get(self.hub.tokens.get(self.token()))
            if existing:
                existing['seen'] = now()
                existing['ip'] = self.client_address[0]
                return self.output({'token': existing['token'], 'id': existing['id']})
            ip = self.client_address[0]
            if self.hub.require_access_code and not self.is_owner():
                attempts = [t for t in self.hub.attempts.get(ip, []) if now() - t < 60]
                if len(attempts) >= 8:
                    raise ApiError('访问码错误次数过多，请一分钟后重试', 429)
                if not secrets.compare_digest(str(data.get('pin', '')), self.hub.pin):
                    if len(self.hub.attempts) < 256:
                        self.hub.attempts[ip] = attempts + [now()]
                    raise ApiError('访问码不正确，请查看主机电脑页面', 403)
            if len(self.hub.devices) >= 64:
                raise ApiError('已达到设备数量上限，请重启服务清理旧设备', 429)
            token, device_id = secrets.token_urlsafe(36), secrets.token_hex(12)
            self.hub.devices[device_id] = {'id': device_id, 'token': token, 'name': self.clean_name(data.get('name')),
                                         'kind': 'phone' if data.get('kind') == 'phone' else 'desktop', 'ip': ip, 'seen': now()}
            self.hub.tokens[token] = device_id
        print('Device joined from:', ip, flush=True)
        self.output({'token': token, 'id': device_id})

    def create_transfer(self, sender, data):
        size = data.get('size')
        if type(size) is not int or not 0 <= size <= LIMIT:
            raise ApiError('单文件上限为 2 GiB', 413)
        request_id = str(data.get('requestId', ''))[:80]
        with self.hub.lock:
            if request_id:
                existing = next((f for f in self.hub.files.values() if f['sender'] == sender['id'] and f['requestId'] == request_id), None)
                if existing:
                    return self.output(self.hub.visible(existing))
            receiver = self.hub.devices.get(str(data.get('receiver', '')))
            if not receiver or receiver['id'] == sender['id'] or now() - receiver['seen'] > 25:
                raise ApiError('接收设备已离线，请等待对方重新加入', 409)
            if len(self.hub.files) >= 1000 or sum(f['size'] for f in self.hub.files.values() if f['status'] not in TERMINAL) + size > TOTAL_LIMIT:
                raise ApiError('临时空间已达 10 GiB 上限，请先清理文件', 413)
            if shutil.disk_usage(self.hub.folder).free < size + 64 * 1024 ** 2:
                raise ApiError('主机磁盘剩余空间不足', 507)
            key = secrets.token_hex(16)
            target = self.hub.folder / key
            target.touch()
            item = {'id': key, 'requestId': request_id, 'sender': sender['id'], 'receiver': receiver['id'],
                    'senderName': sender['name'], 'receiverName': receiver['name'],
                    'name': self.clean_name(data.get('name') or '未命名文件', 255), 'size': size,
                    'bytes': 0, 'status': 'offered', 'created': now(), 'updated': now(), 'sha256': '',
                    'path': target, 'lock': threading.RLock(), 'hash': hashlib.sha256(), 'ticket': None, 'downloads': 0}
            self.hub.files[key] = item
        self.output(self.hub.visible(item))

    def chunk(self, item, device, query):
        if device['id'] != item['sender']:
            raise ApiError('只有发送方可以上传', 403)
        raw = self.body(1048576, binary=True)
        try:
            offset = int(query.get('offset', ['-1'])[0])
        except ValueError:
            raise ApiError('分块偏移不正确')
        if not raw:
            raise ApiError('文件分块为空')
        with item['lock']:
            with self.hub.lock:
                if item['status'] not in ('accepted', 'uploading'):
                    raise ApiError('文件未被接收、已暂停或已取消', 409)
                if offset < 0 or offset + len(raw) > item['size']:
                    raise ApiError('文件分块超出范围', 409)
                previous = item['bytes']
            if offset < previous and offset + len(raw) <= previous:
                with item['path'].open('rb') as existing:
                    existing.seek(offset)
                    if existing.read(len(raw)) != raw:
                        raise ApiError('重复分块内容不一致，请重新发送', 409)
            elif offset == previous:
                with item['path'].open('ab') as target:
                    target.write(raw)
                with self.hub.lock:
                    item['hash'].update(raw)
                    item['bytes'] += len(raw)
                    item['status'], item['updated'] = 'uploading', now()
            else:
                raise ApiError('文件分块顺序不匹配，请重试', 409)
            self.output({'bytes': item['bytes']})

    def file_action(self, item, device, data):
        action, sender = data.get('action'), device['id'] == item['sender']
        with item['lock']:
            if action in ('cancel', 'reject', 'clear'):
                if action == 'reject' and sender:
                    raise ApiError('只有接收方可以拒绝', 403)
                if action == 'clear' and item['status'] not in TERMINAL | {'ready'}:
                    raise ApiError('请先取消正在传输的文件', 409)
                self.hub.release(item, 'rejected' if action == 'reject' else 'cancelled')
            elif action == 'accept' and not sender and item['status'] == 'offered':
                item['status'], item['updated'] = 'accepted', now()
            elif action == 'pause' and sender and item['status'] in ('accepted', 'uploading', 'paused'):
                item['status'], item['updated'] = 'paused', now()
            elif action == 'resume' and sender and item['status'] == 'paused':
                item['status'], item['updated'] = 'accepted', now()
            elif action == 'finish' and sender and item['status'] in ('accepted', 'uploading', 'ready'):
                if item['bytes'] != item['size']:
                    raise ApiError('文件尚未上传完整', 409)
                item['status'], item['updated'] = 'ready', now()
                item['sha256'] = item['hash'].hexdigest()
            else:
                raise ApiError('文件状态已变化，请刷新后重试', 409)
        self.output(self.hub.visible(item))

    def download(self, ticket):
        with self.hub.lock:
            item = next((f for f in self.hub.files.values() if f.get('ticket') and secrets.compare_digest(f['ticket'], ticket)), None)
        if not item or item['status'] != 'ready' or item.get('ticketIp') != self.client_address[0]:
            raise ApiError('下载链接已失效，请在传输记录中重新下载', 404)
        with item['lock']:
            if item['status'] != 'ready':
                raise ApiError('文件已清理', 404)
            source = item['path'].open('rb')
        size, start, end = item['size'], 0, item['size'] - 1
        ranged = self.headers.get('Range')
        try:
            if ranged:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', ranged)
                if not match or not any(match.groups()):
                    raise ValueError()
                left, right = match.groups()
                if left:
                    start, end = int(left), min(int(right), end) if right else end
                else:
                    start = max(0, size - int(right))
                if not size or not 0 <= start <= end:
                    raise ValueError()
        except ValueError:
            source.close()
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        try:
            self.send_response(206 if ranged else 200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(item['name'], safe=''))
            self.send_header('Content-Length', str(max(0, end - start + 1)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if ranged:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command == 'HEAD':
                return
            source.seek(start)
            remaining = max(0, end - start + 1)
            while remaining and not self.hub.stopping.is_set() and item['status'] == 'ready':
                chunk = source.read(min(1048576, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
                item['updated'] = now()
            if remaining == 0 and not ranged:
                with self.hub.lock:
                    item['downloads'] += 1
        finally:
            source.close()

    def host_action(self, path):
        self.require_owner()
        if path == '/api/host/status' and self.command == 'GET':
            return self.output({**self.hub.host_status, 'devices': sum(now() - d['seen'] < 25 for d in self.hub.devices.values()),
                                'pending': sum(f['status'] not in TERMINAL for f in self.hub.files.values()),
                                'used': sum(f['size'] for f in self.hub.files.values() if f['status'] not in TERMINAL)})
        if self.command != 'POST':
            raise ApiError('接口不存在', 404)
        data = self.body()
        if path == '/api/host/access-code':
            return self.output(self.hub.set_access_requirement(data.get('required')))
        if path == '/api/host/firewall':
            from system_tools import request_firewall_repair
            address = str(data.get('ip') or self.hub.address)
            if address not in self.hub.addresses() or not ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback:
                raise ApiError('请选择当前有效的局域网地址')
            return self.output(request_firewall_repair(self.hub.port, address))
        if path == '/api/host/report':
            from system_tools import diagnostic_report
            return self.output({'text': diagnostic_report(self.hub)})
        if path == '/api/host/rotate-code':
            with self.hub.lock:
                if not self.hub.require_access_code:
                    raise ApiError('请先开启访问码')
                self.hub.pin = str(secrets.randbelow(90000000) + 10000000)
                self.hub.attempts.clear()
                return self.output({'accessCode': self.hub.pin})
        if path == '/api/host/shutdown':
            self.output({'ok': True})
            self.hub.stopping.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        raise ApiError('接口不存在', 404)

    def static(self, path):
        target = (ROOT / 'public' / ('index.html' if path == '/' else unquote(path).lstrip('/'))).resolve()
        if not target.is_relative_to((ROOT / 'public').resolve()) or not target.is_file() or target.suffix not in {'.html', '.js', '.css', '.svg'}:
            raise ApiError('页面不存在', 404)
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', (mimetypes.guess_type(target.name)[0] or 'application/octet-stream') + '; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--ip')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Invalid port')
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='linqdrop-hub-') as folder:
        hub = Hub(args.port, folder, args.ip)
        if args.ip and args.ip not in {r['ip'] for r in hub.interfaces} | {'127.0.0.1'}:
            parser.error('The selected IP is not assigned to an active adapter')
        server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
        server.daemon_threads = True
        server.hub = hub
        state = {'pid': os.getpid(), 'port': args.port, 'ip': hub.address, 'python': sys.executable,
                 'boot': hub.boot, 'root': str(ROOT), 'started': hub.started, 'version': VERSION}
        (runtime / 'instance.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        from system_tools import monitor
        threading.Thread(target=monitor, args=(hub,), daemon=True).start()
        print('LinqDrop', VERSION, 'ready on', hub.url(), flush=True)
        if not args.no_browser:
            import webbrowser
            webbrowser.open(f'http://127.0.0.1:{args.port}/')
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            hub.stopping.set()
            server.server_close()
            with hub.lock:
                connections = list(hub.connections)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            for attempt in range(40):
                if not hub.connections:
                    break
                time.sleep(.05)
            try:
                current = json.loads((runtime / 'instance.json').read_text(encoding='utf-8'))
                if current.get('boot') == hub.boot:
                    (runtime / 'instance.json').unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            for item in list(hub.files.values()):
                hub.release(item, 'cancelled')


if __name__ == '__main__':
    main()
