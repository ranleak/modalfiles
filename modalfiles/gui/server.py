"""Local ModalFiles GUI server.

Serves the static frontend and proxies authenticated requests to a remote
ModalFiles WebDAV server. Designed to run on Windows 11 but works on Linux for
development. Stdlib-only so it can run with a bundled Python embeddable.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = 'ModalFiles'
DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / 'static'
REMOTE_API_PREFIX = '/__modalfiles__/api'


def config_dir() -> Path:
    if platform.system() == 'Windows':
        base = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
        return Path(base) / APP_NAME
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base) / 'modalfiles'


def config_path() -> Path:
    return config_dir() / 'config.json'


def load_config() -> dict:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), encoding='utf-8')


def _auth_header(cfg: dict) -> str:
    user = cfg.get('username', '')
    pwd = cfg.get('password', '')
    token = base64.b64encode(f'{user}:{pwd}'.encode('utf-8')).decode('ascii')
    return f'Basic {token}'


def _remote_base(cfg: dict) -> str:
    url = (cfg.get('server_url') or '').strip().rstrip('/')
    return url


def _remote_request(cfg: dict, method: str, path: str, body: bytes | None = None,
                    headers: dict | None = None, timeout: float = 60.0):
    base = _remote_base(cfg)
    if not base:
        raise ValueError('Server URL not configured')
    url = base + path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header('Authorization', _auth_header(cfg))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


class Handler(BaseHTTPRequestHandler):
    server_version = 'ModalFilesGUI/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write(f'[{self.log_date_time_string()}] {fmt % args}\n')

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        n = int(self.headers.get('Content-Length', '0') or '0')
        return self.rfile.read(n) if n > 0 else b''

    def _serve_static(self, path: str):
        if path == '/':
            path = '/index.html'
        # prevent traversal
        rel = path.lstrip('/').split('?', 1)[0]
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        data = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if target.suffix in {'.html'}:
            self.send_header('Cache-Control', 'no-cache')
        else:
            self.send_header('Cache-Control', 'public, max-age=3600')
        self.end_headers()
        self.wfile.write(data)

    def _proxy_remote_api(self, subpath: str):
        cfg = load_config()
        if not _remote_base(cfg):
            self._send_json({'error': 'Server not configured'}, 400)
            return
        qs = ''
        if '?' in self.path:
            qs = '?' + self.path.split('?', 1)[1]
        remote_path = REMOTE_API_PREFIX + subpath + qs
        body = self._read_body() if self.command in ('POST', 'PUT', 'PATCH') else None
        headers = {}
        ct = self.headers.get('Content-Type')
        if ct:
            headers['Content-Type'] = ct
        try:
            resp = _remote_request(cfg, self.command, remote_path, body=body, headers=headers, timeout=120)
        except urllib.error.HTTPError as e:
            raw = e.read()
            self.send_response(e.code)
            self.send_header('Content-Type', e.headers.get('Content-Type', 'application/json'))
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        except Exception as e:
            self._send_json({'error': str(e)}, 502)
            return
        ct = resp.headers.get('Content-Type', 'application/octet-stream')
        # SSE stream proxy
        if 'text/event-stream' in ct:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            return
        raw = resp.read()
        self.send_response(resp.status)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle_config(self):
        if self.command == 'GET':
            cfg = load_config()
            safe = {
                'server_url': cfg.get('server_url', ''),
                'username': cfg.get('username', ''),
                'has_password': bool(cfg.get('password')),
                'drive_letter': cfg.get('drive_letter', 'Z'),
            }
            self._send_json(safe)
            return
        if self.command == 'POST':
            try:
                body = json.loads(self._read_body().decode('utf-8') or '{}')
            except Exception:
                self._send_json({'error': 'invalid json'}, 400)
                return
            cfg = load_config()
            for k in ('server_url', 'username', 'password', 'drive_letter'):
                if k in body:
                    val = body[k]
                    if isinstance(val, str):
                        cfg[k] = val.strip()
            save_config(cfg)
            self._send_json({'ok': True})
            return
        self.send_error(405)

    def _handle_test_connection(self):
        cfg = load_config()
        if not _remote_base(cfg):
            self._send_json({'ok': False, 'error': 'Server not configured'}, 400)
            return
        try:
            resp = _remote_request(cfg, 'GET', REMOTE_API_PREFIX + '/status', timeout=20)
            data = json.loads(resp.read().decode('utf-8'))
            self._send_json({'ok': True, 'status': data})
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', errors='replace')
            self._send_json({'ok': False, 'error': f'HTTP {e.code}', 'detail': raw[:500]}, 200)
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)}, 200)

    def _handle_map_drive(self):
        if platform.system() != 'Windows':
            self._send_json({'ok': False, 'error': 'Drive mapping is only available on Windows'}, 400)
            return
        cfg = load_config()
        url = _remote_base(cfg)
        user = cfg.get('username', '')
        pwd = cfg.get('password', '')
        letter = (cfg.get('drive_letter') or 'Z').strip().upper().rstrip(':')
        if not url or not user or not pwd:
            self._send_json({'ok': False, 'error': 'Configure server URL, username, and password first'}, 400)
            return
        # Build UNC path like the existing installer
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ''
        port = parsed.port
        path = parsed.path or '/'
        path = path.replace('/', '\\')
        if port and port != 443:
            unc = f"\\\\{host}@SSL@{port}\\DavWWWRoot{path}"
        else:
            unc = f"\\\\{host}@SSL\\DavWWWRoot{path}"
        while len(unc) > 3 and unc.endswith('\\'):
            unc = unc[:-1]
        drive = f'{letter}:'
        try:
            # delete existing mapping first (ignore errors)
            subprocess.run(['net', 'use', drive, '/delete', '/y'], capture_output=True, text=True)
            cmd = ['net', 'use', drive, unc, pwd, f'/user:{user}', '/persistent:yes']
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                self._send_json({'ok': True, 'drive': drive, 'unc': unc})
            else:
                msg = (r.stderr or r.stdout or '').strip()
                self._send_json({'ok': False, 'error': msg or f'net use failed ({r.returncode})'})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def _handle_unmap_drive(self):
        if platform.system() != 'Windows':
            self._send_json({'ok': False, 'error': 'Drive mapping is only available on Windows'}, 400)
            return
        cfg = load_config()
        letter = (cfg.get('drive_letter') or 'Z').strip().upper().rstrip(':')
        drive = f'{letter}:'
        try:
            r = subprocess.run(['net', 'use', drive, '/delete', '/y'], capture_output=True, text=True, timeout=20)
            self._send_json({'ok': r.returncode == 0, 'output': (r.stdout or r.stderr).strip()})
        except Exception as e:
            self._send_json({'ok': False, 'error': str(e)})

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', 'http://127.0.0.1')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _dispatch(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == '/local/api/config':
            return self._handle_config()
        if path == '/local/api/test-connection' and self.command == 'POST':
            return self._handle_test_connection()
        if path == '/local/api/map-drive' and self.command == 'POST':
            return self._handle_map_drive()
        if path == '/local/api/unmap-drive' and self.command == 'POST':
            return self._handle_unmap_drive()
        if path.startswith('/local/api/remote'):
            sub = path[len('/local/api/remote'):]
            if not sub.startswith('/'):
                sub = '/' + sub
            return self._proxy_remote_api(sub)
        if path.startswith('/local/'):
            self._send_json({'error': 'not found'}, 404)
            return
        return self._serve_static(path)


def run(port: int = DEFAULT_PORT, open_browser: bool = True):
    mimetypes.add_type('text/css', '.css')
    mimetypes.add_type('application/javascript', '.js')
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = f'http://127.0.0.1:{port}/'
    print(f'{APP_NAME} GUI running at {url}')
    if open_browser:
        threading.Timer(0.4, lambda: _open_app(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _open_app(url: str):
    try:
        if platform.system() == 'Windows':
            # Prefer Edge app mode on Windows 11
            edge = _find_edge()
            if edge:
                subprocess.Popen([edge, f'--app={url}', '--new-window'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            os.startfile(url)  # type: ignore[attr-defined]
            return
        if platform.system() == 'Darwin':
            subprocess.Popen(['open', url])
            return
        subprocess.Popen(['xdg-open', url])
    except Exception:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass


def _find_edge() -> str | None:
    if platform.system() != 'Windows':
        return None
    candidates = [
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description='ModalFiles local GUI server')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    run(port=args.port, open_browser=not args.no_open)


if __name__ == '__main__':
    main()
