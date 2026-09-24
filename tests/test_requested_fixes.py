from unittest.mock import patch,Mock
from datetime import datetime,timedelta,timezone
from backend import db,providers,mobile
from backend.remote_upload import UploadTunnel
from test_workspace import client,png
from fastapi.testclient import TestClient


def test_empty_trend_fetch_preserves_real_previous_and_source_indices(client):
    now=datetime.now(timezone.utc)
    with db.connect() as c:
        for id,when,payload in [('old',(now-timedelta(hours=1)).isoformat(),[{'title':'普通热榜','rank':1},{'title':'Codex 新工具','rank':2}]),('empty',now.isoformat(),[])]:
            c.execute('INSERT INTO trend_samples VALUES(?,?,?,?,?,?,?)',(id,'douyin',id,None,when,'completed',db.dump(payload)))
    all_items=client.get('/api/trends?scope=all').json()[0]
    assert all_items['using_previous'] and all_items['snapshot_id']=='old'
    assert len(all_items['items'])==2 and all_items['items'][1]['source_index']==1
    related=client.get('/api/trends?scope=related').json()[0]
    assert len(related['items'])==1 and related['items'][0]['source_index']==1
    assert client.get('/api/trends?scope=bad').status_code==400


def test_remote_session_routes_only_upload_and_revoke(client):
    with patch.object(mobile,'server',Mock(started=True)),patch('backend.remote_upload.tunnel.start',return_value='https://test.trycloudflare.com') as start:
        result=client.post('/api/upload-sessions',json={'mode':'internet'}).json()
    assert result['mode']=='internet' and result['url'].startswith('https://test.trycloudflare.com/#')
    start.assert_called_once_with(result['id'])
    token=result['url'].split('#')[1]
    with TestClient(mobile.phone) as phone:
        assert phone.get('/api/contents').status_code==404
        assert phone.post('/upload',files={'file':('a.png',png())}).status_code==401
        assert phone.post('/upload',headers={'X-Upload-Token':token},files={'file':('a.png',png())}).status_code==200
        with patch('backend.remote_upload.tunnel.revoke') as revoke:
            assert client.delete('/api/upload-sessions/'+result['id']).status_code==200
            revoke.assert_called_once_with(result['id'])
        assert phone.get('/status',headers={'X-Upload-Token':token}).status_code==401


def test_tunnel_expires_and_does_not_stop_other_live_session():
    tunnel=UploadTunnel();process=Mock();process.poll.return_value=None
    tunnel.process=process;tunnel.url='https://test.trycloudflare.com'
    with patch('backend.remote_upload.time.monotonic',return_value=100):
        tunnel.leases={'a':99,'b':101};tunnel.prune()
        assert tunnel.leases=={'b':101};process.terminate.assert_not_called()
        tunnel.revoke('b');process.terminate.assert_called_once()
        assert tunnel.url is None


def test_semantic_relevance_is_not_keyword_matching(client):
    from backend import trend_relevance,worker
    worker.worker.stop()
    with db.connect() as c:
        c.execute('INSERT INTO trend_samples VALUES(?,?,?,?,?,?,?)',('sample','douyin','semantic',None,db.now(),'completed',db.dump([{'title':'表格自动化让普通员工告别重复录入','rank':1},{'title':'明星演唱会','rank':2}])))
        before=db.one(c,'SELECT * FROM active_model')
    first=trend_relevance.enqueue_analysis();second=trend_relevance.enqueue_analysis()
    assert first['id']==second['id']
    with db.connect() as c:payload=db.load(db.one(c,'SELECT payload FROM jobs WHERE id=?',(first['id'],))['payload'])
    answer={'text':db.dump({'selected':[{'snapshot_id':'sample','index':0,'score':90,'reason':'企业员工的办公自动化实践，符合职场提效内容方向。'}]})}
    with patch('backend.trend_relevance.models.generate_text',return_value=answer):
        trend_relevance.analyze(first['id'],payload)
    display=client.get('/api/trends').json()[0]
    assert len(display['items'])==1 and display['items'][0]['source_index']==0
    assert display['analysis_state']=='completed' and display['items'][0]['relevance_reason']
    assert trend_relevance.enqueue_analysis()['cached']
    with db.connect() as c:assert db.one(c,'SELECT * FROM active_model')==before


def test_trend_history_only_reports_real_comparable_samples(client):
    now=datetime.now(timezone.utc)
    with db.connect() as c:
        for index,heat in enumerate([100,120]):
            at=(now-timedelta(minutes=2-index)).isoformat()
            c.execute('INSERT INTO trend_samples VALUES(?,?,?,?,?,?,?)',(f'history-{index}','douyin',f'history-{index}',None,at,'completed',db.dump([{'title':'办公自动化技巧','rank':3,'heat':heat},{'title':'单次采样','rank':5,'heat':None}])))
    board=client.get('/api/trends?scope=all').json()[0]
    item=next(x for x in board['items'] if x['title']=='办公自动化技巧')
    assert item['sample_hits']==2 and item['heat_change_percent']==20
    assert [x['heat'] for x in item['heat_history']]==[100,120]
    single=next(x for x in board['items'] if x['title']=='单次采样')
    assert single['heat_change_percent'] is None and single['heat_history']==[]


def test_redfox_xhs_rank_response_is_saved_without_invented_items(client):
    post=Mock(status_code=200)
    post.json.return_value={'code':2000,'data':{'dyList':[],'bzList':[],'zhList':[]}}
    get=Mock(status_code=200)
    get.json.return_value={'code':2000,'data':[{'title':'AI办公真实案例','index':'4','hotCount':'12.5w','url':'https://www.xiaohongshu.com/search_result?keyword=AI','gmtCreate':'2026-09-23 10:00:00'}]}
    with patch.object(providers,'provider_secret',return_value='test'),patch.object(providers,'reserve_call'),patch.object(providers.httpx,'post',return_value=post),patch.object(providers.httpx,'get',return_value=get) as request_get:
        result=providers.fetch_trends({'start':'2026-09-23 09:00:00','end':'2026-09-23 10:00:00','include_xiaohongshu':True})
    assert result['xiaohongshu']==1
    assert request_get.call_args.kwargs['params']['platform']==6
    board=next(x for x in client.get('/api/trends?scope=all').json() if x['platform']=='xiaohongshu')
    assert board['items'][0]['title']=='AI办公真实案例'
    assert board['items'][0]['heat']== '12.5w'
    assert board['items'][0]['heat_history'][0]['heat']==125000
