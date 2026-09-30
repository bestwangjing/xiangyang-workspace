from unittest.mock import patch

from backend import db,reports,worker
from test_workspace import client,make_publication,png


def add_screenshot_metrics(client,publication_id,values,observed='2026-09-20T12:00:00+08:00'):
    evidence=client.post('/api/updates/screenshots',files={'file':(db.uid()+'.png',png(),'image/png')}).json()
    payload=dict(publication_id=publication_id,scope='cumulative',metrics=values,observed_at=observed)
    response=client.post('/api/evidence/'+evidence['id']+'/confirm',json=payload)
    assert response.status_code==200,response.text
    return evidence['id']


def test_post_review_import_requires_screenshot_and_locks_after_completion(client):
    worker.worker.stop()
    post=make_publication('xiaohongshu',title='待复盘优秀帖子')
    imported=client.post('/api/post-reviews/import',json={'publication_ids':[post['id']]})
    assert imported.status_code==200 and imported.json()['imported']==1
    assert client.post('/api/post-reviews/'+post['id']+'/start',json={}).status_code==400

    add_screenshot_metrics(client,post['id'],dict(impressions=2000,views=300,likes=30,saves=42,comments=8,new_follows=5,click_rate=15,watch_seconds=38))
    queued=client.post('/api/post-reviews/'+post['id']+'/start',json={})
    assert queued.status_code==200,queued.text
    with db.connect() as c:
        job=db.one(c,'SELECT * FROM jobs WHERE id=?',(queued.json()['id'],))
        payload=db.load(job['payload'])
    model_report=dict(summary='内容价值不错，点击仍可提升',score=76,confidence=88,strengths=['收藏表现较好'],
        p0=[dict(title='封面利益点不够集中',evidence='点击率低于同类样本',action='把结果放到封面第一视觉',metric='下一篇封面点击率')],
        p1=[dict(title='强化关注理由',evidence='涨粉转化仍有空间',action='增加系列预告',metric='每千阅读涨粉')],
        metric_analysis=[dict(metric='收藏率',value='14%',baseline='账号同类基线待补',judgement='表现较好',action='保留步骤清单')],
        facts=['后台截图已提供'],inferences=['封面可能限制点击'],unknowns=[],counterexamples=['单篇样本不代表稳定规律'],experiments=['下一篇只测试封面'])
    with patch('backend.models.generate_text',return_value=dict(text=db.dump(model_report),model='test')):
        result=reports.generate(job['id'],'review',payload)
    library=client.get('/api/post-reviews').json()
    assert library[0]['review_status']=='reviewed' and library[0]['review_id']==result['id']
    assert client.post('/api/post-reviews/'+post['id']+'/start',json={}).status_code==400
    detail=client.get('/api/post-reviews/'+post['id']+'/report').json()
    assert detail['report_json']['score']==76 and detail['publication']['title']=='待复盘优秀帖子'


def test_single_and_linked_experience_are_user_confirmed_and_traceable(client):
    worker.worker.stop()
    before=make_publication('xiaohongshu',title='优化前帖子')
    after=make_publication('xiaohongshu',title='优化后帖子')
    add_screenshot_metrics(client,before['id'],dict(impressions=1000,views=82,saves=5,click_rate=8.2),observed='2026-09-17T12:00:00+08:00')
    add_screenshot_metrics(client,after['id'],dict(impressions=1000,views=126,saves=15,click_rate=12.6),observed='2026-09-22T12:00:00+08:00')

    queued=client.post('/api/experience-analyses',json={'mode':'linked','sources':{'before':before['id'],'after':after['id']}})
    assert queued.status_code==200,queued.text
    with db.connect() as c:
        job=db.one(c,'SELECT * FROM jobs WHERE id=?',(queued.json()['id'],));payload=db.load(job['payload'])
    assert [x['role'] for x in payload['sources']]==['before','after']
    assert payload['comparability']['same_platform'] and payload['comparability']['same_format']

    analysis=dict(diagnosis='具体结果前置后，点击与收藏同步提升',confidence=88,
        success_factors=[dict(title='具体结果前置',evidence='点击率8.2%提升到12.6%',content_change='封面由项目名改为具体结果')],
        metric_comparison=[dict(metric='封面点击率',before='8.2%',after='12.6%',delta='+4.4pp',judgement='明显提升')],
        caveats=['前后对比说明强相关，不等于严格因果'],
        draft=dict(title='具体结果前置提升点击',category='封面与标题',formats=['image_post'],conclusion='封面先展示具体结果更容易获得点击',conditions='工具教程与方法分享',limitations='需保持主题和观察周期可比'))
    with patch('backend.models.generate_text',return_value=dict(text=db.dump(analysis),model='test')):
        result=reports.generate(job['id'],'experience',payload)
    with db.connect() as c:c.execute("UPDATE jobs SET state='completed',result=?,updated_at=? WHERE id=?",(db.dump(result),db.now(),job['id']))
    assert client.get('/api/experiences').json()==[]
    saved=client.post('/api/experiences',json=dict(job_id=job['id'],title=analysis['draft']['title'],category='封面与标题',formats=['image_post'],conclusion=analysis['draft']['conclusion'],conditions=analysis['draft']['conditions'],limitations=analysis['draft']['limitations']))
    assert saved.status_code==200,saved.text
    rows=client.get('/api/experiences').json()
    assert len(rows)==1 and rows[0]['mode']=='linked'
    assert [x['role'] for x in rows[0]['sources']]==['before','after']
    assert client.post('/api/experiences',json=dict(job_id=job['id'],title='重复',category='封面',formats=['image_post'],conclusion='重复',conditions='重复')).status_code==400

    same=client.post('/api/experience-analyses',json={'mode':'linked','sources':{'before':before['id'],'after':before['id']}})
    assert same.status_code==400
