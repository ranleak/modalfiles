import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE_SCRIPT = REPO_ROOT / 'scripts' / 'prepare-python-embed.py'


def _load_prepare_module():
    spec = importlib.util.spec_from_file_location('prepare_python_embed', PREPARE_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_mock_embed_zip(path: Path, stem: str = 'python314', *,
                         pth_text: str | None = None,
                         include_vcruntime: bool = True,
                         extra_files: dict[str, bytes] | None = None) -> None:
    if pth_text is None:
        pth_text = f'{stem}.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n'
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('python.exe', b'MZ')
        zf.writestr('pythonw.exe', b'MZ')
        zf.writestr(f'{stem}.dll', b'MZ')
        zf.writestr(f'{stem}.zip', b'PK')
        zf.writestr(f'{stem}._pth', pth_text.encode('utf-8'))
        if include_vcruntime:
            zf.writestr('vcruntime140.dll', b'MZ')
            zf.writestr('vcruntime140_1.dll', b'MZ')
        for k, v in (extra_files or {}).items():
            zf.writestr(k, v)


class TestPreparePythonEmbed(unittest.TestCase):
    def test_patch_pth_adds_parent_and_keeps_site_disabled(self):
        mod = _load_prepare_module()
        with tempfile.TemporaryDirectory() as td:
            pth = Path(td) / 'python314._pth'
            pth.write_text('python314.zip\n.\n\n#import site\n', encoding='utf-8')
            lines = mod.patch_pth(pth)
            self.assertEqual(lines, ['python314.zip', '.', '..', '', '#import site'])
            self.assertEqual(pth.read_text(encoding='utf-8'),
                             'python314.zip\n.\n..\n\n#import site\n')

            # Idempotent on second call
            lines2 = mod.patch_pth(pth)
            self.assertEqual(lines2, lines)

    def test_patch_pth_comments_bare_import_site(self):
        mod = _load_prepare_module()
        with tempfile.TemporaryDirectory() as td:
            pth = Path(td) / 'python314._pth'
            pth.write_text('python314.zip\nimport site\n', encoding='utf-8')
            lines = mod.patch_pth(pth)
            self.assertIn('# import site', lines)
            self.assertNotIn('import site', lines)
            self.assertIn('..', lines)

    def test_prepare_stages_runtime_and_cleans_stale_files(self):
        mod = _load_prepare_module()
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            zip_path = td_path / 'embed.zip'
            dest = td_path / 'python'
            dest.mkdir()
            (dest / 'python312._pth').write_text('stale\n', encoding='utf-8')
            (dest / 'stale.dll').write_bytes(b'old')

            _make_mock_embed_zip(zip_path, stem='python314')
            mod.prepare(zip_path, dest)

            self.assertFalse((dest / 'python312._pth').exists())
            self.assertFalse((dest / 'stale.dll').exists())
            self.assertTrue((dest / 'pythonw.exe').is_file())
            self.assertTrue((dest / 'python314.dll').is_file())
            pth_lines = (dest / 'python314._pth').read_text(encoding='utf-8').splitlines()
            self.assertIn('..', pth_lines)

    def test_prepare_rejects_zip_slip(self):
        mod = _load_prepare_module()
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            zip_path = td_path / 'evil.zip'
            dest = td_path / 'python'
            _make_mock_embed_zip(zip_path, extra_files={'../escape.txt': b'bad'})
            with self.assertRaises(SystemExit):
                mod.prepare(zip_path, dest)
            self.assertFalse((td_path / 'escape.txt').exists())

    def test_prepare_requires_vcruntime(self):
        mod = _load_prepare_module()
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            zip_path = td_path / 'missing_vc.zip'
            dest = td_path / 'python'
            _make_mock_embed_zip(zip_path, include_vcruntime=False)
            with self.assertRaises(SystemExit):
                mod.prepare(zip_path, dest)


class TestGuiServerEmbedCompatibility(unittest.TestCase):
    def test_gui_package_uses_only_embed_safe_stdlib(self):
        excluded_in_embed = {
            'tkinter', 'idlelib', 'test', 'tests', 'venv', 'ensurepip',
            'turtle', 'turtledemo',
        }
        files = [
            REPO_ROOT / 'modalfiles' / '__init__.py',
            REPO_ROOT / 'modalfiles' / 'gui' / '__init__.py',
            REPO_ROOT / 'modalfiles' / 'gui' / 'server.py',
        ]
        for f in files:
            tree = ast.parse(f.read_text(encoding='utf-8'), filename=str(f))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split('.')[0]
                        self.assertIn(top, sys.stdlib_module_names, f'{f}: {top}')
                        self.assertNotIn(top, excluded_in_embed, f'{f}: {top}')
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    top = node.module.split('.')[0]
                    if top == '__future__':
                        continue
                    self.assertIn(top, sys.stdlib_module_names, f'{f}: {top}')
                    self.assertNotIn(top, excluded_in_embed, f'{f}: {top}')

    def test_isolated_import_without_site(self):
        # Verify modalfiles.gui.server imports cleanly under -I -S (no site, no user/cwd sys.path)
        # when the install root is explicitly added as ._pth would do.
        code = (
            "import sys; "
            f"sys.path.append({str(REPO_ROOT)!r}); "
            "import modalfiles.gui.server as s; "
            "assert s.STATIC_DIR.is_dir()"
        )
        proc = subprocess.run(
            [sys.executable, '-I', '-S', '-c', code],
            cwd='/tmp',
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestGuiServerRuntime(unittest.TestCase):
    def test_setup_stdio_and_log_message_handle_none_streams(self):
        from modalfiles.gui import server as gui_server

        with tempfile.TemporaryDirectory() as td:
            old_xdg = os.environ.get('XDG_CONFIG_HOME')
            old_out, old_err = sys.stdout, sys.stderr
            try:
                os.environ['XDG_CONFIG_HOME'] = td
                sys.stdout = None
                sys.stderr = None
                gui_server._setup_stdio()
                self.assertIsNotNone(sys.stdout)
                self.assertIsNotNone(sys.stderr)
                sys.stderr.write('hello pythonw log\n')
                sys.stderr.flush()
                log_path = gui_server.config_dir() / 'gui.log'
                self.assertTrue(log_path.is_file())
                self.assertIn('hello pythonw log', log_path.read_text(encoding='utf-8'))
                if sys.stderr not in (old_out, old_err):
                    sys.stderr.close()

                # Handler.log_message must also survive if sys.stderr is None
                handler = gui_server.Handler.__new__(gui_server.Handler)
                handler.log_date_time_string = lambda: 'now'
                sys.stderr = None
                handler.log_message('%s', 'ignored')
            finally:
                sys.stdout = old_out
                sys.stderr = old_err
                if old_xdg is None:
                    os.environ.pop('XDG_CONFIG_HOME', None)
                else:
                    os.environ['XDG_CONFIG_HOME'] = old_xdg

    def test_gui_server_static_config_proxy_and_already_running(self):
        from modalfiles.gui import server as gui_server

        # Mock remote ModalFiles server
        class MockRemote(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                if self.headers.get('Authorization') != 'Basic dTpw':
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.path == '/__modalfiles__/api/status':
                    data = json.dumps({'uptime_s': 12.5, 'requests_total': 3}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path == '/__modalfiles__/api/metrics':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b'data: {"uptime_s": 1}\n\n')
                    self.wfile.flush()
                    return
                self.send_error(404)

        remote = ThreadingHTTPServer(('127.0.0.1', 0), MockRemote)
        remote_thread = threading.Thread(target=remote.serve_forever, daemon=True)
        remote_thread.start()
        remote_port = remote.server_address[1]

        gui = gui_server._GUIServer(('127.0.0.1', 0), gui_server.Handler)
        gui_thread = threading.Thread(target=gui.serve_forever, daemon=True)
        gui_thread.start()
        gui_port = gui.server_address[1]
        base = f'http://127.0.0.1:{gui_port}'

        old_xdg = os.environ.get('XDG_CONFIG_HOME')
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ['XDG_CONFIG_HOME'] = td

                with urllib.request.urlopen(f'{base}/') as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn(b'ModalFiles', r.read())

                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f'{base}/../pyproject.toml')
                self.assertEqual(ctx.exception.code, 404)

                req = urllib.request.Request(
                    f'{base}/local/api/config',
                    data=json.dumps({
                        'server_url': f'http://127.0.0.1:{remote_port}',
                        'username': 'u',
                        'password': 'p',
                        'drive_letter': 'M',
                    }).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(json.loads(r.read())['ok'], True)

                with urllib.request.urlopen(f'{base}/local/api/config') as r:
                    cfg = json.loads(r.read())
                    self.assertEqual(cfg['username'], 'u')
                    self.assertTrue(cfg['has_password'])
                    self.assertNotIn('password', cfg)

                req_test = urllib.request.Request(f'{base}/local/api/test-connection',
                                                  data=b'', method='POST')
                with urllib.request.urlopen(req_test) as r:
                    res = json.loads(r.read())
                    self.assertTrue(res['ok'])
                    self.assertEqual(res['status']['requests_total'], 3)

                with urllib.request.urlopen(f'{base}/local/api/remote/metrics', timeout=5) as r:
                    line = r.readline().decode()
                    self.assertIn('"uptime_s": 1', line)

                self.assertTrue(gui_server._is_gui_running(gui_port))
                opened = []
                with mock.patch.object(gui_server, '_open_app', side_effect=opened.append):
                    gui_server.run(port=gui_port, open_browser=True)
                self.assertEqual(opened, [f'http://127.0.0.1:{gui_port}/'])
        finally:
            gui.shutdown()
            gui.server_close()
            remote.shutdown()
            remote.server_close()
            if old_xdg is None:
                os.environ.pop('XDG_CONFIG_HOME', None)
            else:
                os.environ['XDG_CONFIG_HOME'] = old_xdg


class TestWindowsPackagingFiles(unittest.TestCase):
    def test_launcher_prefers_bundled_python(self):
        c_src = (REPO_ROOT / 'windows' / 'gui_launcher.c').read_text(encoding='utf-8')
        bundled_idx = c_src.index('python\\\\pythonw.exe')
        venv_idx = c_src.index('venv\\\\Scripts\\\\pythonw.exe')
        pyw_idx = c_src.index('L"pyw.exe"')
        self.assertLess(bundled_idx, venv_idx)
        self.assertLess(venv_idx, pyw_idx)

    def test_nsi_bundles_and_uninstalls_python(self):
        nsi = (REPO_ROOT / 'windows' / 'ModalFiles.nsi').read_text(encoding='utf-8')
        self.assertIn('PYTHON_EMBED_DIR', nsi)
        self.assertIn('SetOutPath "$INSTDIR\\python"', nsi)
        self.assertIn('RMDir /r "$INSTDIR\\python"', nsi)

    def test_build_windows_script_stages_bundle_and_verifies_sha256(self):
        import hashlib
        import shutil
        import stat

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            work = td_path / 'repo'
            shutil.copytree(
                REPO_ROOT,
                work,
                ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', 'dist', '.cache'),
            )
            embed_zip = td_path / 'python-3.14.10-embed-amd64.zip'
            _make_mock_embed_zip(embed_zip, stem='python314')
            digest = hashlib.sha256(embed_zip.read_bytes()).hexdigest()

            bindir = td_path / 'bin'
            bindir.mkdir()
            cc = bindir / 'x86_64-w64-mingw32-gcc'
            cc.write_text(
                '#!/usr/bin/env python3\n'
                'import sys, pathlib\n'
                'out = sys.argv[sys.argv.index("-o") + 1]\n'
                'pathlib.Path(out).write_bytes(b"MZ")\n',
                encoding='utf-8',
            )
            cc.chmod(cc.stat().st_mode | stat.S_IEXEC)

            nsis = bindir / 'makensis'
            nsis.write_text(
                '#!/usr/bin/env python3\n'
                'import sys, pathlib\n'
                'dist = [a.split("=", 1)[1] for a in sys.argv if a.startswith("-DDIST_DIR=")][0]\n'
                'ver = [a.split("=", 1)[1] for a in sys.argv if a.startswith("-DVERSION=")][0]\n'
                'emb = [a.split("=", 1)[1] for a in sys.argv if a.startswith("-DPYTHON_EMBED_DIR=")][0]\n'
                'assert (pathlib.Path(emb) / "pythonw.exe").is_file()\n'
                'pathlib.Path(dist, f"ModalFiles-Setup-{ver}.exe").write_bytes(b"MZ")\n',
                encoding='utf-8',
            )
            nsis.chmod(nsis.stat().st_mode | stat.S_IEXEC)

            env = os.environ.copy()
            env['PATH'] = f'{bindir}:{env["PATH"]}'
            env['PYTHON_EMBED_ZIP'] = str(embed_zip)
            env['PYTHON_EMBED_SHA256'] = digest

            res = subprocess.run(
                ['./scripts/build-windows.sh'],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((work / 'dist' / 'ModalFilesGUI' / 'ModalFilesGUI.exe').is_file())
            self.assertTrue((work / 'dist' / 'ModalFilesGUI' / 'python' / 'pythonw.exe').is_file())
            self.assertTrue((work / 'dist' / 'ModalFilesGUI' / 'python' / 'python314._pth').is_file())
            self.assertTrue((work / 'dist' / 'ModalFilesGUI' / 'modalfiles' / 'gui' / 'server.py').is_file())
            self.assertTrue((work / 'dist' / 'ModalFilesGUI-0.2.0-win64.zip').is_file())
            self.assertTrue((work / 'dist' / 'ModalFiles-Setup-0.2.0.exe').is_file())

            # Bad SHA-256 must fail fast
            env['PYTHON_EMBED_SHA256'] = '0' * 64
            bad = subprocess.run(
                ['./scripts/build-windows.sh'],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(bad.returncode, 0)


if __name__ == '__main__':
    unittest.main()
