"""Authenticated WebDAV backed by a Modal Volume.

A single request at a time is deliberate: Volume reload requires no open files,
while WebDAV file responses may keep a file open until their last ASGI message.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import contextlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from wsgidav.wsgidav_app import WsgiDAVApp

log = logging.getLogger(__name__)
MAX_ZIP_ENTRIES = 10000
MAX_ZIP_BYTES = 10 * 1024**3
API_PREFIX = '/__modalfiles__/api'

_metrics = {
    'started_at': time.time(),
    'requests_total': 0,
    'requests_active': 0,
    'bytes_in': 0,
    'bytes_out': 0,
    'errors_total': 0,
    'mutations_total': 0,
    'zip_jobs_total': 0,
    'zip_jobs_active': 0,
}
_metrics_lock = asyncio.Lock()
_log_buffer: collections.deque[dict] = collections.deque(maxlen=2000)
_log_seq = 0
_log_cond = asyncio.Condition()
_zip_jobs: dict[str, dict] = {}
_zip_jobs_lock = asyncio.Lock()


class _BufferLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _log_seq
        try:
            entry = {
                'seq': 0,
                'ts': record.created,
                'level': record.levelname,
                'logger': record.name,
                'msg': self.format(record),
            }
        except Exception:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def _push() -> None:
            global _log_seq
            _log_seq += 1
            entry['seq'] = _log_seq
            _log_buffer.append(entry)

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_push)
        else:
            _push()


def _install_log_handler() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _BufferLogHandler):
            return
    handler = _BufferLogHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


async def _metric_inc(**changes: int) -> None:
    async with _metrics_lock:
        for k, v in changes.items():
            _metrics[k] = _metrics.get(k, 0) + v


async def _metric_snapshot() -> dict:
    async with _metrics_lock:
        snap = dict(_metrics)
    snap['uptime_s'] = max(0.0, time.time() - snap['started_at'])
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        snap['cpu_user_s'] = ru.ru_utime
        snap['cpu_sys_s'] = ru.ru_stime
        snap['max_rss_kb'] = ru.ru_maxrss
    except Exception:
        pass
    try:
        load1, load5, load15 = os.getloadavg()
        snap['loadavg'] = [load1, load5, load15]
    except Exception:
        pass
    try:
        import shutil as _sh
        usage = _sh.disk_usage('/')
        snap['disk_total'] = usage.total
        snap['disk_used'] = usage.used
        snap['disk_free'] = usage.free
    except Exception:
        pass
    return snap


def _check_basic_auth(scope, username: str, password: str) -> bool:
    headers = dict(scope.get('headers', []))
    auth = headers.get(b'authorization', b'')
    if not auth.startswith(b'Basic '):
        return False
    try:
        decoded = base64.b64decode(auth.split(b' ', 1)[1]).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        return False
    if ':' not in decoded:
        return False
    u, p = decoded.split(':', 1)
    return u == username and p == password


def _unauthorized():
    return JSONResponse({'detail': 'Unauthorized'}, status_code=401,
                        headers={'WWW-Authenticate': 'Basic realm="ModalFiles"'})


def _safe_resolve(root: Path, rel: str) -> Path | None:
    rel = rel.lstrip('/')
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def extract_zip(archive: Path) -> bool:
    """Extract to a new sibling directory, never overwrite existing files.

    Unsafe, corrupt, huge and encrypted archives remain available as ZIPs. The
    temporary directory is removed on failure, so no partial folder is exposed.
    """
    if archive.suffix.lower() != '.zip' or not archive.is_file():
        return False
    target = archive.with_suffix('')
    if target.exists():
        return False
    temp = None
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
                raise ValueError('archive exceeds extraction limits')
            for info in infos:
                name = info.filename
                p = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (not name or '\\' in name or p.is_absolute() or '..' in p.parts
                        or (p.parts and ':' in p.parts[0]) or stat.S_ISLNK(mode)
                        or info.flag_bits & 1):
                    raise ValueError('unsafe or encrypted ZIP member')
            temp = Path(tempfile.mkdtemp(prefix='.modalfiles-extract-', dir=archive.parent))
            z.extractall(temp)
        # All requests are serialized in this container; never replace a folder.
        if target.exists():
            return False
        temp.rename(target)
        temp = None
        return True
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        log.exception('ZIP extraction failed for %s', archive.name)
        return False
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)



def _find_7z() -> str | None:
    for name in ('7zz', '7z', '7za'):
        path = shutil.which(name)
        if path:
            return path
    return None


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob('*'):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _run_cloud_zip_sync(root: Path, folder_rel: str, job_id: str, volume, lock: threading.RLock, _loop=None) -> None:
    """Run cloud ZIP in a worker thread. Uses loop.call_soon_threadsafe for lock/metrics."""
    def _metric_inc_sync(**changes: int) -> None:
        # best-effort sync update; metrics_lock is async so we just mutate under GIL for counters
        for k, v in changes.items():
            _metrics[k] = _metrics.get(k, 0) + v

    _metric_inc_sync(zip_jobs_active=1, zip_jobs_total=1)
    job = _zip_jobs[job_id]
    tmp_zip = None
    try:
        folder = _safe_resolve(root, folder_rel)
        if folder is None or not folder.exists() or not folder.is_dir():
            raise ValueError('Folder not found')
        if folder.resolve() == root.resolve():
            raise ValueError('Cannot compress the root folder')
        zip_path = folder.with_suffix('.zip')
        if zip_path.exists():
            raise ValueError(f'ZIP already exists: {zip_path.name}')
        job['status'] = 'running'
        job['stage'] = 'scanning'
        job['folder'] = folder_rel
        job['zip_name'] = zip_path.name
        job['total_bytes'] = _dir_size(folder)
        job['stage'] = 'compressing'
        seven = _find_7z()
        tmp_dir = tempfile.mkdtemp(prefix='modalfiles-zip-')
        tmp_zip = Path(tmp_dir) / (zip_path.name + '.tmp')
        try:
            if seven:
                cmd = [seven, 'a', '-tzip', '-mx=5', '-y', str(tmp_zip), '.']
                proc = subprocess.Popen(
                    cmd, cwd=str(folder),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors='replace')
                job['tool'] = os.path.basename(seven)
                assert proc.stdout is not None
                for line in proc.stdout:
                    text = line.strip()
                    if not text:
                        continue
                    job['last_line'] = text[-300:]
                    for part in text.split():
                        if part.endswith('%') and part[:-1].isdigit():
                            job['percent'] = max(0, min(100, int(part[:-1])))
                            break
                rc = proc.wait()
                if rc != 0:
                    raise RuntimeError(f'7zip exited with code {rc}')
            else:
                job['tool'] = 'zipfile'
                _zip_folder_python(folder, tmp_zip, job)
            job['stage'] = 'finalizing'
            job['percent'] = 99

            # Move into volume and commit under the DAV lock.
            with lock:
                if volume is not None:
                    volume.reload()
                if zip_path.exists():
                    raise ValueError(f'ZIP already exists: {zip_path.name}')
                shutil.move(str(tmp_zip), str(zip_path))
                tmp_zip = None
                if volume is not None:
                    job['stage'] = 'committing'
                    volume.commit()

            job['percent'] = 100
            job['status'] = 'done'
            job['stage'] = 'done'
            job['zip_path'] = '/' + str(zip_path.relative_to(root)).replace(os.sep, '/')
            try:
                job['zip_size'] = zip_path.stat().st_size
            except OSError:
                job['zip_size'] = 0
            log.info('Cloud ZIP complete: %s -> %s', folder_rel, job['zip_path'])
        finally:
            with contextlib.suppress(Exception):
                if tmp_zip and tmp_zip.exists():
                    tmp_zip.unlink()
            with contextlib.suppress(Exception):
                shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        job['status'] = 'error'
        job['stage'] = 'error'
        job['error'] = str(e)
        log.exception('Cloud ZIP failed for %s', folder_rel)
    finally:
        job['finished_at'] = time.time()
        _metric_inc_sync(zip_jobs_active=-1)


def _zip_folder_python(folder: Path, tmp_zip: Path, job: dict) -> None:
    total = job.get('total_bytes') or 1
    done = 0
    with zipfile.ZipFile(tmp_zip, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for p in sorted(folder.rglob('*')):
            if p.is_symlink():
                continue
            arc = p.relative_to(folder)
            if p.is_dir():
                z.writestr(str(arc) + '/', '')
            elif p.is_file():
                z.write(p, str(arc))
                try:
                    done += p.stat().st_size
                except OSError:
                    pass
                job['percent'] = min(98, int(done * 98 / total))
                job['last_line'] = str(arc)[-300:]


def _list_files(root: Path, rel: str) -> dict:
    target = _safe_resolve(root, rel)
    if target is None or not target.exists():
        return {'error': 'not_found'}
    if target.is_file():
        st = target.stat()
        return {'path': '/' + str(target.relative_to(root)).replace(os.sep, '/'),
                'kind': 'file', 'size': st.st_size, 'mtime': st.st_mtime}
    entries = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as e:
        return {'error': str(e)}
    for p in children:
        if p.name.startswith('.modalfiles-'):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        is_dir = p.is_dir()
        entry = {
            'name': p.name,
            'path': '/' + str(p.relative_to(root)).replace(os.sep, '/'),
            'kind': 'dir' if is_dir else 'file',
            'size': st.st_size if not is_dir else None,
            'mtime': st.st_mtime,
        }
        if is_dir:
            zip_sibling = p.with_suffix('.zip')
            entry['has_zip'] = zip_sibling.exists() and zip_sibling.is_file()
            try:
                # shallow child count, cheap
                entry['children'] = sum(1 for _ in p.iterdir())
            except OSError:
                entry['children'] = None
        entries.append(entry)
    return {
        'path': '/' + str(target.relative_to(root)).replace(os.sep, '/'),
        'kind': 'dir',
        'entries': entries,
    }


def create_app(root: str, username: str, password: str, volume=None):
    if not username or not password:
        raise ValueError('MODALFILES_USER and MODALFILES_PASSWORD must be nonempty')
    _install_log_handler()
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    dav = WsgiDAVApp({
        'provider_mapping': {'/': str(root_path)},
        'http_authenticator': {'accept_basic': True, 'accept_digest': False,
                               'default_to_digest': False},
        'simple_dc': {'user_mapping': {'*': {username: {'password': password}}}},
        'lock_storage': True,
        'verbose': 1,
        'dir_browser': {'enable': False},
    })
    dav_asgi = WSGIMiddleware(dav)
    lock = threading.RLock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def serve_dav(scope, receive, send):
        if scope['type'] != 'http':
            return await dav_asgi(scope, receive, send)
        with lock:
            if volume is not None:
                await asyncio.to_thread(volume.reload)
            method = scope['method'].upper()
            mutating = method in {'PUT', 'DELETE', 'MKCOL', 'COPY', 'MOVE', 'PROPPATCH'}
            messages = []
            status = 500
            async def capture(message):
                nonlocal status
                if message['type'] == 'http.response.start':
                    status = message['status']
                if mutating:
                    messages.append(message)
                else:
                    await send(message)
            await dav_asgi(scope, receive, capture)
            if mutating:
                if 200 <= status < 300:
                    await _metric_inc(mutations_total=1)
                    if method in {'PUT', 'MOVE'}:
                        path = scope.get('path', '')
                        if method == 'MOVE':
                            headers = dict(scope.get('headers', []))
                            path = unquote(urlsplit(headers.get(b'destination', b'').decode('latin1')).path)
                        candidate = (root_path / path.lstrip('/')).resolve()
                        if candidate.is_relative_to(root_path):
                            await asyncio.to_thread(extract_zip, candidate)
                    if volume is not None:
                        await asyncio.to_thread(volume.commit)
                for message in messages:
                    await send(message)

    def _auth_or_401(scope):
        if not _check_basic_auth(scope, username, password):
            return _unauthorized()
        return None

    @app.get(API_PREFIX + '/status')
    async def api_status(request: Request):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        snap = await _metric_snapshot()
        snap['volume_attached'] = volume is not None
        snap['seven_zip'] = _find_7z() is not None
        return JSONResponse(snap)

    @app.get(API_PREFIX + '/metrics')
    async def api_metrics(request: Request):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        async def stream():
            while True:
                if await request.is_disconnected():
                    break
                snap = await _metric_snapshot()
                yield f"data: {json.dumps(snap)}\n\n"
                await asyncio.sleep(1.0)
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.get(API_PREFIX + '/logs')
    async def api_logs(request: Request):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        after = int(request.query_params.get('after', '0') or '0')
        async def stream():
            last = after
            # send recent history first
            hist = [e for e in list(_log_buffer) if e['seq'] > last]
            for e in hist[-200:]:
                yield f"data: {json.dumps(e)}\n\n"
                last = max(last, e['seq'])
            while True:
                if await request.is_disconnected():
                    break
                new = [e for e in list(_log_buffer) if e['seq'] > last]
                if not new:
                    try:
                        await asyncio.wait_for(asyncio.sleep(1.0), timeout=1.0)
                    except Exception:
                        pass
                    # heartbeat
                    yield ": ping\n\n"
                    continue
                for e in new:
                    yield f"data: {json.dumps(e)}\n\n"
                    last = max(last, e['seq'])
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.get(API_PREFIX + '/files')
    async def api_files(request: Request, path: str = '/'):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        with lock:
            if volume is not None:
                await asyncio.to_thread(volume.reload)
            result = await asyncio.to_thread(_list_files, root_path, path)
        if 'error' in result:
            code = 404 if result['error'] == 'not_found' else 400
            return JSONResponse(result, status_code=code)
        return JSONResponse(result)

    @app.post(API_PREFIX + '/zip')
    async def api_zip(request: Request):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({'error': 'invalid json'}, status_code=400)
        folder = (body.get('path') or '').strip()
        if not folder:
            return JSONResponse({'error': 'path is required'}, status_code=400)
        async with _zip_jobs_lock:
            # prevent duplicate active job for same folder
            for j in _zip_jobs.values():
                if j.get('folder') == folder and j.get('status') in ('queued', 'running'):
                    return JSONResponse({'error': 'zip job already running for this folder', 'job': j}, status_code=409)
            job_id = uuid.uuid4().hex[:12]
            job = {
                'id': job_id,
                'folder': folder,
                'status': 'queued',
                'stage': 'queued',
                'percent': 0,
                'total_bytes': 0,
                'created_at': time.time(),
                'finished_at': None,
                'error': None,
                'zip_path': None,
                'zip_size': 0,
                'tool': None,
                'last_line': '',
            }
            _zip_jobs[job_id] = job
        t = threading.Thread(target=_run_cloud_zip_sync,
                             args=(root_path, folder, job_id, volume, lock, None),
                             daemon=True)
        t.start()
        return JSONResponse({'job': job}, status_code=202)

    @app.get(API_PREFIX + '/zip/jobs')
    async def api_zip_jobs(request: Request):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        async with _zip_jobs_lock:
            jobs = sorted(_zip_jobs.values(), key=lambda j: j.get('created_at', 0), reverse=True)
        return JSONResponse({'jobs': jobs[:50]})

    @app.get(API_PREFIX + '/zip/jobs/{job_id}')
    async def api_zip_job(request: Request, job_id: str):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        job = _zip_jobs.get(job_id)
        if not job:
            return JSONResponse({'error': 'not found'}, status_code=404)
        return JSONResponse({'job': job})

    @app.api_route(API_PREFIX + '/{path:path}', methods=['GET','POST','PUT','DELETE','OPTIONS','HEAD','PATCH'])
    async def api_fallback(request: Request, path: str):
        if not _check_basic_auth(request.scope, username, password):
            return _unauthorized()
        return JSONResponse({'error': 'not found'}, status_code=404)

    async def dispatch(scope, receive, send):
        if scope['type'] != 'http':
            return await serve_dav(scope, receive, send)
        path = scope.get('path', '')
        if path == API_PREFIX or path.startswith(API_PREFIX + '/'):
            return await app(scope, receive, send)
        # DAV path: count metrics around it
        await _metric_inc(requests_total=1, requests_active=1)
        method = scope.get('method', '').upper()
        req_bytes = 0
        resp_bytes = 0

        async def counting_receive():
            msg = await receive()
            nonlocal req_bytes
            if msg['type'] == 'http.request':
                req_bytes += len(msg.get('body', b'') or b'')
            return msg

        async def counting_send(message):
            nonlocal resp_bytes
            if message['type'] == 'http.response.body':
                resp_bytes += len(message.get('body', b'') or b'')
            elif message['type'] == 'http.response.start':
                status = message.get('status', 500)
                if status >= 400:
                    await _metric_inc(errors_total=1)
            await send(message)

        try:
            await serve_dav(scope, counting_receive, counting_send)
        finally:
            await _metric_inc(requests_active=-1, bytes_in=req_bytes, bytes_out=resp_bytes)

    return dispatch
