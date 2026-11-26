import io
import zipfile
from fastapi.testclient import TestClient
from modalfiles.service import create_app


class Volume:
    def __init__(self):
        self.reloads = self.commits = 0
    def reload(self):
        self.reloads += 1
    def commit(self):
        self.commits += 1


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return output.getvalue()


def test_dav_and_zip(tmp_path):
    volume = Volume()
    client = TestClient(create_app(str(tmp_path), 'alice', 'secret', volume), base_url='https://example.com')
    assert client.request('PROPFIND', '/', headers={'Depth': '1'}).status_code == 401
    client.auth = ('alice', 'secret')
    assert client.request('OPTIONS', '/').status_code == 200
    assert client.put('/hello.txt', content=b'hello').status_code == 201
    assert client.get('/hello.txt').content == b'hello'
    response = client.request('PROPFIND', '/', headers={'Depth': '1'})
    assert response.status_code == 207 and b'hello.txt' in response.content
    assert client.put('/test.zip', content=archive({'folder/a.txt': 'ok'})).status_code == 201
    assert client.get('/test/folder/a.txt').text == 'ok'
    assert client.request('MOVE', '/hello.txt', headers={'Destination': 'https://example.com/moved.txt'}).status_code == 201
    assert client.delete('/moved.txt').status_code == 204
    assert volume.commits == 4
    assert volume.reloads >= 9


def test_unsafe_zip_and_no_overwrite(tmp_path):
    client = TestClient(create_app(str(tmp_path), 'a', 'b'), base_url='https://example.com')
    client.auth = ('a', 'b')
    assert client.put('/bad.zip', content=archive({'../escape': 'bad'})).status_code == 201
    assert not (tmp_path.parent / 'escape').exists()
    assert not (tmp_path / 'bad').exists()
    (tmp_path / 'existing').mkdir()
    (tmp_path / 'existing' / 'keep').write_text('keep')
    assert client.put('/existing.zip', content=archive({'keep': 'overwrite'})).status_code == 201
    assert (tmp_path / 'existing' / 'keep').read_text() == 'keep'


def test_move_zip_triggers_extraction(tmp_path):
    client = TestClient(create_app(str(tmp_path), 'a', 'b'), base_url='https://example.com')
    client.auth = ('a', 'b')
    assert client.put('/pending.tmp', content=archive({'x': 'data'})).status_code == 201
    assert client.request('MOVE', '/pending.tmp', headers={'Destination': 'https://example.com/done.zip'}).status_code == 201
    assert client.get('/done/x').text == 'data'


def test_api_status_and_auth(tmp_path):
    client = TestClient(create_app(str(tmp_path), 'u', 'p'), base_url='https://example.com')
    r = client.get('/__modalfiles__/api/status')
    assert r.status_code == 401
    client.auth = ('u', 'p')
    r = client.get('/__modalfiles__/api/status')
    assert r.status_code == 200
    body = r.json()
    assert body['requests_total'] >= 0
    assert 'bytes_in' in body and 'bytes_out' in body
    assert 'uptime_s' in body


def test_api_files_and_cloud_zip(tmp_path):
    client = TestClient(create_app(str(tmp_path), 'u', 'p'), base_url='https://example.com')
    client.auth = ('u', 'p')
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'a.txt').write_text('hello')
    r = client.get('/__modalfiles__/api/files?path=/')
    assert r.status_code == 200
    entries = {e['name']: e for e in r.json()['entries']}
    assert 'docs' in entries
    assert entries['docs']['kind'] == 'dir'
    assert entries['docs']['has_zip'] is False

    r = client.post('/__modalfiles__/api/zip', json={'path': '/docs'})
    assert r.status_code == 202
    job_id = r.json()['job']['id']
    # poll for completion
    for _ in range(60):
        jr = client.get(f'/__modalfiles__/api/zip/jobs/{job_id}')
        assert jr.status_code == 200
        job = jr.json()['job']
        if job['status'] in ('done', 'error'):
            break
        import time; time.sleep(0.1)
    assert job['status'] == 'done', job
    assert (tmp_path / 'docs.zip').exists()
    # folder should now report has_zip
    r2 = client.get('/__modalfiles__/api/files?path=/')
    entries2 = {e['name']: e for e in r2.json()['entries']}
    assert entries2['docs']['has_zip'] is True
