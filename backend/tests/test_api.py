import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

TEST_DIR = tempfile.TemporaryDirectory(prefix="atelier-tests-")
os.environ["ATELIER_DATA_DIR"] = TEST_DIR.name
os.environ["ADMIN_USERNAME"] = "test-admin"
os.environ["ADMIN_PASSWORD"] = "test-secret"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app, base_url="https://testserver") as client:
        login_admin(client)
        yield client
    TEST_DIR.cleanup()


def pdf(text="Invoice 2024 - 15/03/2024"):
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        return doc.tobytes()


def extract(raw):
    with fitz.open(stream=raw, filetype="pdf") as doc:
        return ''.join(p.get_text() for p in doc)


def replace(client, files=None, rules=None, **options):
    return client.post('/api/replace', files=files or [('files', ('example.pdf', pdf(), 'application/pdf'))],
                       data={'equivalences': json.dumps(rules if rules is not None else [{'query': '2024', 'replace': '2025'}]), **options})


def login_admin(client):
    response = client.post('/admin/login', data={'user':'test-admin', 'secret':'test-secret'}, follow_redirects=False)
    assert response.status_code == 303
    return response


def test_docs_and_protection(client):
    client.cookies.clear()
    assert client.get('/').status_code == 200
    assert 'href="/api/docs"' in client.get('/').text
    assert client.get('/api/docs').status_code == 200
    schema = client.get('/api/openapi.json').json()
    assert '/api/replace' in schema['paths']
    assert '/api/admin/operations' not in schema['paths']
    for route in ['/admin', '/admin/']:
        response = client.get(route)
        assert response.status_code == 200
        assert response.url.path == '/admin/login'
        assert 'autocomplete="username"' in response.text
        assert 'type="password"' in response.text
        assert '<script' not in response.text
        assert 'www-authenticate' not in response.headers
        assert 'Journal des opérations' not in response.text
    response = client.get('/api/admin/operations')
    assert response.status_code == 401
    assert 'www-authenticate' not in response.headers
    response = client.post('/admin/login', data={'user':'test-admin', 'secret':'bad'})
    assert response.status_code == 401
    assert 'Utilisateur ou mot de passe incorrect.' in response.text
    assert 'www-authenticate' not in response.headers
    assert 'test-secret' not in response.text
    response = login_admin(client)
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
    for route in ['/admin', '/admin/', '/api/admin/operations']:
        result = client.get(route)
        assert result.status_code == 200
        assert result.headers['cache-control'] == 'no-store'
    assert client.get('/admin/login').url.path == '/admin'
    for route in ['/.env', '/data/operations.sqlite3', '/templates/admin.html', '/admin.html', '/templates/login.html']:
        assert client.get(route).status_code == 404


def test_session_revocation_expiry_and_csrf(client):
    from audit import ADMIN_COOKIE
    login_admin(client)
    cookie = client.cookies.get(ADMIN_COOKIE)
    assert client.post('/admin/logout', headers={'Origin':'https://attacker.example'}, follow_redirects=False).status_code == 403
    assert client.get('/api/admin/operations').status_code == 200
    response = client.post('/admin/logout', headers={'Origin':'https://testserver'}, follow_redirects=False)
    assert response.status_code == 303
    assert ADMIN_COOKIE not in client.cookies
    assert client.get('/api/admin/operations', headers={'Cookie':ADMIN_COOKIE+'='+cookie}).status_code == 401
    assert client.post('/admin/login', data={'user':'test-admin','secret':'test-secret'}, headers={'Origin':'https://attacker.example'}).status_code == 403
    login_admin(client)
    with main._audit_store.connect() as db:
        db.execute('UPDATE admin_sessions SET expires=0')
    assert client.get('/api/admin/operations').status_code == 401
    login_admin(client)
    with TestClient(main.app, base_url='https://testserver') as other_client:
        # Server-side sessions work across independent client connections.
        assert other_client.get('/api/admin/operations', headers={'Cookie':ADMIN_COOKIE+'='+client.cookies.get(ADMIN_COOKIE)}).status_code == 200
    assert client.get('/api/admin/operations', headers={'Cookie':ADMIN_COOKIE+'=forged'}).status_code == 401


def test_single_replacement_and_cleanup(client):
    before = set(Path('/tmp').glob('atelier-api-*'))
    sessions = set(Path(main.LOTS_DIR).iterdir())
    result = replace(client)
    assert result.status_code == 200, result.text
    assert result.headers['content-type'] == 'application/pdf'
    assert result.headers['x-replacements-applied'] == '2'
    assert '2025' in extract(result.content)
    assert '2024' not in extract(result.content)
    assert set(Path('/tmp').glob('atelier-api-*')) == before
    assert set(Path(main.LOTS_DIR).iterdir()) == sessions
    records = client.get('/api/admin/operations', params={'q': result.headers['x-operation-id']}).json()
    record = records['items'][0]
    assert record['source'] == 'api'
    assert record['applied'] == 2
    assert record['equivalences'][0]['replace'] == '2025'
    assert '%PDF-' not in json.dumps(record)
    assert 'pdf_bytes' not in json.dumps(record)


def test_zip_multiple_rules_and_duplicate_names(client):
    files = [('files', (name, pdf('Invoice 2024'), 'application/pdf')) for name in ['a.pdf','a.pdf','a_1 (2).pdf']]
    result = replace(client, files=files, rules=[{'query':'2024','replace':'2025'}, {'query':'Invoice','replace':'Receipt'}])
    assert result.status_code == 200, result.text
    assert result.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())) == 4
        report = json.loads(archive.read('report.json'))
        assert len(report['documents']) == 3
        for item in report['documents']:
            assert 'Receipt 2025' in extract(archive.read(item['output_filename']))


@pytest.mark.parametrize('rules', [[], [{'query':'x','replace':'y'}]*25, [{'kind':'regex','query':'[','replace':'x'}], [{'query':''}], [{'query':'x','unknown':True}]])
def test_invalid_rules(client, rules):
    assert replace(client, rules=rules).status_code == 422


def test_invalid_pdf_and_missing_fields(client):
    before = set(Path('/tmp').glob('atelier-api-*'))
    assert replace(client, files=[('files', ('bad.pdf', b'not a pdf'))]).status_code == 400
    assert replace(client, files=[('files', ('bad.pdf', b'%PDF-corrupt'))]).status_code == 422
    assert client.post('/api/replace').status_code == 422
    assert client.get('/api/lots/unknown/invalid-route').status_code == 404
    assert set(Path('/tmp').glob('atelier-api-*')) == before
    assert client.get('/api/admin/operations?status=error').json()['total'] >= 3


def test_no_match_and_dates(client):
    original = pdf()
    result = replace(client, files=[('files', ('original.pdf', original))], rules=[{'query':'absent','replace':'other'}])
    assert result.content == original
    assert result.status_code == 200
    assert result.headers['x-replacements-applied'] == '0'
    assert '2024' in extract(result.content)
    result = replace(client, rules=[{'kind':'dates'}], date_rule=json.dumps({'type':'shift','years':1}))
    assert result.status_code == 200, result.text
    assert '15/03/2025' in extract(result.content)


def test_platform_workflow_and_audit(client):
    headers={'X-Atelier-Source':'plateforme'}
    response = client.post('/api/lots', files=[('files', ('platform.pdf', pdf(), 'application/pdf'))], headers=headers)
    assert response.status_code == 200, response.text
    sid = response.json()['id']
    rules = {'finders':[{'id':'f1','query':'2024','replace':'2026'}]}
    for action in ['scan','simulate','apply']:
        result = client.post(f'/api/lots/{sid}/{action}', json=rules, headers=headers)
        assert result.status_code == 200, result.text
    result = client.get(f'/api/lots/{sid}/documents/d0/download', headers=headers)
    assert '2026' in extract(result.content)
    assert client.delete(f'/api/lots/{sid}', headers=headers).status_code == 200
    records = client.get('/api/admin/operations', params={'source':'plateforme','q':sid}).json()['items']
    assert {'upload','scan','simulate','apply','download_one','drop_lot'} <= {r['event'] for r in records}
    assert next(r for r in records if r['event']=='apply')['rules']==main.EditRequest(**rules).model_dump()


def test_pagination_and_legacy_import():
    from audit import AuditStore
    with tempfile.TemporaryDirectory() as folder:
        legacy = Path(folder)/'usage.log'
        legacy.write_text(json.dumps({'t':100,'event':'upload','names':['old.pdf']})+'\ninvalid\n')
        store = AuditStore(Path(folder)/'audit.db', legacy)
        AuditStore(Path(folder)/'audit.db', legacy)
        for i in range(5):
            store.write({'t':200+i,'event':'test','status':200,'source':'api'})
        assert store.query(1, 2, '', '', '')['total']==6
        assert len(store.query(2, 2, '', '', '')['items'])==2
        assert store.query(1, 50, 'historique', '', '')['total']==1
        assert store.query(1, 50, '', '', "' OR 1=1 --")['total']==0


def test_limits_and_refused_replacements(client, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(main, 'MAX_FILE_BYTES', 16)
        assert replace(client).status_code == 413
    with monkeypatch.context() as patch:
        patch.setattr(main, 'MAX_LOT_BYTES', 16)
        assert replace(client).status_code == 413
    assert replace(client, files=[('files', ('empty.pdf', b''))]).status_code == 400
    assert replace(client, files=[('files', ('a.pdf', pdf()))] * 25).status_code == 413
    response = replace(client, rules=[{'query':'Invoice', 'replace':'😀'}], auto_repair='false')
    assert response.status_code == 422, response.text
    assert response.json()['detail']['documents'][0]['error']


def test_worker_timeout_cleanup(client, monkeypatch):
    import public_api
    class FakeProcess:
        returncode = None
        killed = False
        async def wait(self):
            if not self.killed:
                raise public_api.asyncio.TimeoutError()
            return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -9
    process = FakeProcess()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(public_api.asyncio, 'create_subprocess_exec', spawn)
    before = set(Path('/tmp').glob('atelier-api-*'))
    assert replace(client).status_code == 504
    assert process.killed
    assert set(Path('/tmp').glob('atelier-api-*')) == before
