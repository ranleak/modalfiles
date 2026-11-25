"""Authenticated WebDAV backed by a Modal Volume.

A single request at a time is deliberate: Volume reload requires no open files,
while WebDAV file responses may keep a file open until their last ASGI message.
"""
import asyncio
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

from a2wsgi import WSGIMiddleware
from fastapi import FastAPI
from wsgidav.wsgidav_app import WsgiDAVApp

log = logging.getLogger(__name__)
MAX_ZIP_ENTRIES = 10000
MAX_ZIP_BYTES = 10 * 1024**3


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


def create_app(root: str, username: str, password: str, volume=None):
    if not username or not password:
        raise ValueError('MODALFILES_USER and MODALFILES_PASSWORD must be nonempty')
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
    asgi = WSGIMiddleware(dav)
    lock = asyncio.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def serve(scope, receive, send):
        if scope['type'] != 'http':
            return await asgi(scope, receive, send)
        async with lock:
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
            await asgi(scope, receive, capture)
            if mutating:
                if 200 <= status < 300:
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
    app.mount('/', serve)
    return app
