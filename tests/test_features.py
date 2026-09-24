from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from backend import db,reports
from backend import worker,providers
from test_workspace import client,make_publication,png

def publication(client,platform='douyin',title='同名测试'):
    return make_publication(platform,title=title)

def test_publication_overrides_and_precise_time_are_independent(client):
    p=publication(client)
    response=client.patch('/api/publications/'+p['id'],json=dict(title='抖音独立标题',body_text='抖音正文',paid_status='paid',expected_version=1))
    assert response.status_code==200,response.text
    edited=client.get('/api/publications').json()[0]
    assert edited['title']=='抖音独立标题' and edited['paid_status']=='paid'
    assert client.patch('/api/publications/'+p['id'],json=dict(title='旧窗口',expected_version=1)).status_code==409
    evidence=client.post('/api/updates/screenshots',files={'file':('x.png',png())}).json()
    payload=dict(evidence_id=evidence['id'],expected_version=2,precision='minute',local_date='2026-09-15',instant='2026-09-15T10:30:00+08:00')
    response=client.post('/api/publications/'+p['id']+'/publication-time/confirm',json=payload)
    assert response.status_code==200,response.text
    assert client.get('/api/publications').json()[0]['published_at']=='2026-09-15T10:30:00+08:00'
    assert reports.compute([p['id']])['items'][0]['paid_status']=='paid'

def test_trend_ratios_use_comparable_coverage_and_source_dedupe(client):
    now=datetime.now(timezone.utc)
    with db.connect() as c:
        for index,days,title in [(1,1,'AI工具'),(2,2,'其他'),(3,8,'AI工作')]:
            t=(now-timedelta(days=days)).isoformat()
            c.execute('INSERT INTO trend_samples VALUES(?,?,?,?,?,?,?)',(str(index),'douyin',str(index),t,t,'completed',db.dump([dict(title=title,heat=100,url='https://www.douyin.com/')])) )
    row=client.get('/api/trends?days=7').json()[0]
    keyword=next(x for x in row['keywords'] if x['keyword']=='AI')
    assert keyword['ratio']==.5 and keyword['baseline_ratio']==1 and keyword['change_percent']==-50
    first=client.post('/api/topics/from-source',json=dict(kind='trend',id='1',index=0)).json()
    second=client.post('/api/topics/from-source',json=dict(kind='trend',id='1',index=0)).json()
    assert first['id']==second['id'] and second['duplicate']

def test_budget_cancellation_and_evidence_backed_scores(client):
    import pytest
    worker.worker.stop()
    assert client.put('/api/provider-budgets/redfox',json={'daily_call_limit':1}).status_code==200
    providers.reserve_call('redfox','test')
    with pytest.raises(providers.ProviderError,match='上限'):providers.reserve_call('redfox','blocked')
    assert client.get('/api/provider-budgets').json()[0]['used']==1
    job=worker.enqueue('model_test',{})
    assert client.post('/api/jobs/'+job['id']+'/cancel').json()['state']=='cancelled'
    weights=db.TOPIC['weights']
    item=dict(ratings={k:dict(value=8,reason='测试依据',evidence_ids=['source']) for k in weights})
    assert reports.topic_score(item,weights,{'source'})==80
    assert reports.topic_score(item,weights,set()) is None

def test_export_retry_does_not_repeat_generation(client):
    worker.worker.stop()
    with db.connect() as c:snapshot=db.snapshot(c)
    payload=dict(snapshot=snapshot,config_version_id='codex-v1',thoughts='验收输入')
    with patch('backend.models.generate_text',return_value=dict(text='# 测试方案',model='test')) as generation:
        with patch('backend.reports.write_export',side_effect=OSError('disk test')):
            import pytest
            with pytest.raises(OSError):reports.generate('export-retry','plan',payload)
        result=reports.generate('export-retry','plan',payload)
        assert result['resumed_export'] and generation.call_count==1
    assert len(client.get('/api/plans').json())==1

def test_week_review_does_not_use_future_snapshots(client):
    worker.worker.stop();p=publication(client)
    account=client.get('/api/dashboard').json()['accounts'][0]
    with db.connect() as c:
        for index,(day,subject,values) in enumerate([
            ('2026-09-12',('account_id',account['id']),{'fans':100}),
            ('2026-09-19',('account_id',account['id']),{'fans':120}),
            ('2026-09-19',('publication_id',p['id']),{'views':500}),
            ('2026-09-20',('publication_id',p['id']),{'views':900})]):
            id='week-'+str(index)
            c.execute('INSERT INTO metric_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)',(id,subject[1] if subject[0]=='publication_id' else None,subject[1] if subject[0]=='account_id' else None,None,'fixture',day+'T12:00:00+08:00',None,None,'cumulative',id,db.now()))
            for key,num in values.items():c.execute('INSERT INTO metric_values VALUES(?,?,?,?,0)',(id,key,num,'count'))
    response=client.post('/api/reviews',json=dict(kind='week',publication_ids=[p['id']],start_date='2026-09-13',end_date='2026-09-19'))
    assert response.status_code==200,response.text
    with db.connect() as c:payload=db.load(db.one(c,'SELECT payload FROM jobs WHERE id=?',(response.json()['id'],))['payload'])
    assert payload['computed']['items'][0]['metrics']['views']==500
    assert payload['computed']['accounts'][0]['period_endpoints']['net_fans']==20
