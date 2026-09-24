import io
import sqlite3
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from backend import db
from backend.main import app
from backend.providers import eligible
from backend.metrics import number

@pytest.fixture
def client(tmp_path,monkeypatch):
    root=tmp_path/'data-root'
    root.mkdir()
    monkeypatch.setattr(db,'ROOT',root)
    with TestClient(app) as c:
        token=c.get('/api/session').json()['csrf'];c.headers['x-csrf-token']=token
        yield c

def png():
    b=io.BytesIO();Image.new('RGB',(80,90),'white').save(b,format='PNG');return b.getvalue()

def make_publication(platform='xiaohongshu',title='同名测试',post_id=None,published_local_date=None):
    """Local manuscripts are gone: platform publications are created by auto sync."""
    post_id=post_id or db.uid()
    canonical={'xiaohongshu':'https://www.xiaohongshu.com/explore/','douyin':'https://www.douyin.com/video/'}[platform]+post_id
    with db.connect() as c:
        account=db.one(c,'SELECT id FROM accounts WHERE platform=?',(platform,))
        id=db.uid()
        c.execute("INSERT INTO publications(id,content_id,account_id,platform,platform_post_id,canonical_url,status,title_override,published_local_date,published_precision,published_time_status) VALUES(?,NULL,?,?,?,?,'published',?,?,'unknown','unknown')",
                  (id,account['id'],platform,post_id,canonical,title,published_local_date))
    return dict(id=id,platform=platform,platform_post_id=post_id,title=title)

def test_profile_disk_version_and_atomic_rules(client):
    p=client.get('/api/profile').json()
    value={**p['profile'],'keywords':['新关键词'],'exclusions':[],'expected_profile_version':p['profile_version'],'expected_topic_rule_version':p['topic_version']}
    value['xiaohongshu_name']='持久化账号'
    assert client.put('/api/profile',json=value).status_code==200
    assert client.put('/api/profile',json=value).status_code==409
    db.initialize()
    result=client.get('/api/profile').json()
    assert result['profile']['xiaohongshu_name']=='持久化账号'
    assert result['rules']['keywords']==['新关键词'] and result['rules']['weights']==db.TOPIC['weights']
    assert db.path().is_file()
    value['expected_profile_version']=2;value['expected_topic_rule_version']=2;value['weekly_target']=31
    assert client.put('/api/profile',json=value).status_code==422
    assert client.get('/api/profile').json()['profile_version']==2

def test_publications_listed_without_local_manuscript(client):
    make_publication('xiaohongshu',title='第一篇笔记')
    make_publication('douyin',title='第一支视频')
    rows=client.get('/api/publications').json()
    assert len(rows)==2 and all(r['content_id'] is None for r in rows)
    assert {r['platform'] for r in rows}=={'xiaohongshu','douyin'}
    assert all(r['media']==[] for r in rows)

def test_screenshot_dedupe_confirm_unknown_and_time(client):
    pub=make_publication('xiaohongshu',title='截图更新测试')
    evidence=client.post('/api/updates/screenshots',files={'file':('a.png',png(),'image/png')}).json()
    repeated=client.post('/api/updates/screenshots',files={'file':('b.png',png(),'image/png')}).json()
    assert evidence['id']==repeated['id'] and repeated['duplicate']
    payload=dict(publication_id=pub['id'],scope='cumulative',metrics={'views':1000,'likes':None,'new_follows':5},published_local_date='2026-09-15',expected_version=1)
    r=client.post('/api/evidence/'+evidence['id']+'/confirm',json=payload);assert r.status_code==200,r.text
    assert client.post('/api/evidence/'+evidence['id']+'/confirm',json=payload).json()['duplicate']
    p=client.get('/api/publications').json()[0]
    assert p['published_local_date']=='2026-09-15' and 'likes' not in p['metrics']['values']
    assert p['metrics']['observed_at'] is None
    from backend.reports import compute
    assert compute([pub['id']])['items'][0]['follows_per_1000']==5
    with db.connect() as c: assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0]==1

def test_csrf_origin_and_upload_type(client):
    assert client.post('/api/storage/backup',headers={'x-csrf-token':'invalid'}).status_code==403
    assert client.get('/api/profile',headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/updates/screenshots',files={'file':('a.png',b'<script>evil</script>','image/png')}).status_code==400
    assert client.get('/api/profile',headers={'Host':'evil.example'}).status_code==403

def test_model_unique_activation_and_busy(client):
    cfg=client.get('/api/models/configs').json();assert cfg['active']['config_id']=='codex'
    r=client.patch('/api/models/configs/codex',json={'name':'测试 Codex','provider':'codex','expected_version':1,'model_id':''});assert r.status_code==200,r.text
    vid=r.json()['version_id']
    assert client.get('/api/models/configs').json()['active']['config_version_id']=='codex-v1'
    assert client.post('/api/models/activate',json={'config_version_id':vid,'expected_revision':1}).status_code==200
    assert client.post('/api/models/activate',json={'config_version_id':'codex-v1','expected_revision':1}).status_code==409
    with db.connect() as c:
        c.execute("INSERT INTO jobs(id,type,request_key,payload_hash,payload,state,created_at,updated_at) VALUES('busy','review','busy','x','{}','recovery_required',?,?)",(db.now(),db.now()))
    assert client.post('/api/models/activate',json={'config_version_id':'codex-v1','expected_revision':2}).status_code==409
    assert client.delete('/api/models/configs/codex').status_code==409

def test_low_fan_and_numeric_boundaries():
    assert eligible(4999,501)
    assert not eligible(5000,501)
    assert not eligible(4999,500)
    assert not eligible(None,1000)
    assert number('1.2万')==12000 and number('1万+') is None

def test_database_backup_and_missing_database_guard(client,tmp_path):
    result=client.post('/api/storage/backup').json();assert not result['includes_media']
    with sqlite3.connect(db.ROOT/'backups'/result['filename']) as c: assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    # Simulate database loss without deleting anything.
    db.path().rename(db.path().with_suffix('.saved'))
    with pytest.raises(RuntimeError,match='缺失'): db.initialize()
