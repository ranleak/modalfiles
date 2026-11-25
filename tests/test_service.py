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
