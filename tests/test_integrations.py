import hashlib
import json
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import db,reports
from backend.mobile import phone
from backend.providers import protect,save_secret,get_secret
from test_workspace import client,make_publication,png

def test_phone_expiry_limits_and_management_isolation(client):
    token='a-valid-test-token';id=db.uid()
    with db.connect() as c: c.execute('INSERT INTO upload_sessions VALUES(?,?,?,NULL,0)',(id,hashlib.sha256(token.encode()).hexdigest(),(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()))
    with TestClient(phone) as p:
        assert p.get('/api/profile').status_code==404
        assert p.post('/upload',files={'file':('x.png',png())}).status_code==401
        assert p.post('/upload',headers={'X-Upload-Token':token},files={'file':('x.png',png())}).status_code==200
        assert p.get('/status',headers={'X-Upload-Token':token}).json()['count']==1
        with db.connect() as c: c.execute('UPDATE upload_sessions SET count=20 WHERE id=?',(id,))
        assert p.post('/upload',headers={'X-Upload-Token':token},files={'file':('x.png',png())}).status_code==429
        with db.connect() as c: c.execute('UPDATE upload_sessions SET expires_at=? WHERE id=?',('2000-01-01T00:00:00+00:00',id))
        assert p.get('/status',headers={'X-Upload-Token':token}).status_code==401

def test_secrets_not_in_database_or_api(client,tmp_path,monkeypatch):
    from backend import providers
    monkeypatch.setattr(providers,'secret_root',lambda:tmp_path/'secrets')
    secret='only-test-not-real-credential-ABC'
    id=save_secret('compatible',secret)
    assert get_secret(id)==secret
    assert secret.encode() not in db.path().read_bytes()
    response=client.get('/api/models/configs')
    assert response.status_code==200
    assert secret not in response.text
    assert protect(protect(b'test'),True)==b'test'

def test_ocr_candidate_units_and_date_labels():
    from backend.metrics import parse_ocr
    blocks=[dict(text='播放量',confidence=99,polygon=[]),dict(text='1.2万',confidence=98,polygon=[]),dict(text='发布时间 2026年09月15日',confidence=99,polygon=[]),dict(text='统计日期 2026-09-19',confidence=99,polygon=[])]
    result=parse_ocr(blocks)
    assert result['metrics'][0]['value']==12000
    assert len(result['publication_times'])==1 and result['publication_times'][0]['date']=='2026-09-15'
    assert result['requires_confirmation']

def test_metric_corrections_preserve_original(client):
    account=client.get('/api/dashboard').json()['accounts'][0]
    evidence=client.post('/api/updates/screenshots',files={'file':('a.png',png())}).json()
    payload=dict(account_id=account['id'],scope='cumulative',metrics={'fans':100})
    first=client.post('/api/evidence/'+evidence['id']+'/confirm',json=payload).json()
    payload['metrics']['fans']=120;payload['correction_reason']='截图人工校正'
    assert client.post('/api/evidence/'+evidence['id']+'/confirm',json=payload).json()['corrected']
    with db.connect() as c:
        assert c.execute('SELECT value FROM metric_values WHERE snapshot_id=?',(first['id'],)).fetchone()[0]==100
        assert c.execute('SELECT new_value FROM metric_corrections').fetchone()[0]==120
    assert client.get('/api/dashboard').json()['accounts'][0]['metrics']['values']['fans']==120

def test_generation_exports_escape_and_immutable_versions(client):
    worker_stop()
    pub=make_publication('douyin',title='复盘作品')
    with db.connect() as c: snap=db.snapshot(c)
    payload=dict(snapshot=snap,config_version_id='codex-v1',kind='single',publication_ids=[pub['id']],computed=reports.compute([pub['id']]))
    model=dict(text=json.dumps(dict(facts=['<script>alert(1)</script>'],inferences=[],unknowns=['没有观测值'],counterexamples=[],experiments=['先补数据'])),model='test-only')
    with patch('backend.models.generate_text',return_value=model): result=reports.generate('test-job','review',payload)
    report=client.get('/api/reviews/'+result['id']).json()
    assert '&lt;script&gt;' in report['html_text'] and '<script>' not in report['html_text']
    assert json.loads(report['metadata'])['profile_version']==snap['profile_version']
    assert 'test-only' in client.get('/api/reviews/'+result['id']+'/export').text

def worker_stop():
    from backend import worker
    worker.worker.stop()
