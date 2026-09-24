import pytest
from unittest.mock import patch
from backend import db,worker
from backend import screenshot_batch as batch
from backend.providers import ProviderError
from test_workspace import client,make_publication,png

TITLE='WorkBuddy的三种隐藏用法'

def setup(client,title=TITLE):
    worker.worker.stop()
    pub=make_publication('xiaohongshu',title=title,post_id='synced-post-1')
    id=client.post('/api/updates/screenshots',files={'file':('a.png',png())}).json()['id']
    blocks=[{'text':t,'confidence':99} for t in [title,'观看','123','点赞','12']]
    proposal={'platform':'xiaohongshu','records':[{'kind':'post','title_indices':[0],'scope':'cumulative','metrics':[
        {'key':'views','label_index':1,'value_index':2,'value_text':'123','confidence':.99},
        {'key':'likes','label_index':3,'value_index':4,'value_text':'12','confidence':.99}]}]}
    job=batch.start_batch({'platform':'xiaohongshu','evidence_ids':[id]})
    with db.connect() as c:
        c.execute("UPDATE jobs SET state='running' WHERE id=?",(job['id'],))
        payload=db.load(db.one(c,'SELECT payload FROM jobs WHERE id=?',(job['id'],))['payload'])
    return id,job,payload,{'blocks':blocks},proposal,pub

def test_auto_update_matches_synced_publication_and_is_idempotent(client):
    id,job,payload,parsed,proposal,pub=setup(client)
    with patch.object(batch.providers,'ocr',return_value=parsed),patch.object(batch.models,'generate_text',return_value={'text':db.dump(proposal)}) as generate:
        result=batch.run_batch(job['id'],payload)
        assert result['done']==1 and result['updated']==1
        batch.run_batch(job['id'],payload)
        generate.assert_called_once()
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publications').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0]==1
        snap=db.one(c,'SELECT * FROM metric_snapshots')
        assert snap['source']=='screenshot_auto' and snap['publication_id']==pub['id'] and snap['observed_at'] is None
        assert db.one(c,'SELECT published_at FROM publications')['published_at'] is None
        assert dict(c.execute('SELECT metric_key,value FROM metric_values').fetchall())=={'views':123,'likes':12}
    assert client.get('/api/evidence').json()[0]['updated_platform']=='xiaohongshu'
    progress=client.get('/api/jobs').json()[0]['progress']
    assert progress['done']==1 and progress['total']==1
    assert client.post('/api/updates/batch',json={'platform':'douyin','evidence_ids':[id]}).status_code==400

@pytest.mark.parametrize('case',['wrong_platform','ambiguous','period','fabricated'])
def test_unsafe_extractions_do_not_update(client,case):
    id,job,payload,parsed,proposal,pub=setup(client)
    if case=='wrong_platform':proposal['platform']='douyin'
    if case=='period':proposal['records'][0]['scope']='period'
    if case=='fabricated':
        for m in proposal['records'][0]['metrics']:m['value_text']='999999'
    if case=='ambiguous':
        make_publication('xiaohongshu',title=TITLE,post_id='synced-post-2')
    with patch.object(batch.providers,'ocr',return_value=parsed),patch.object(batch.models,'generate_text',return_value={'text':db.dump(proposal)}):
        result=batch.run_batch(job['id'],payload)
    assert result['attention']==1 and result['updated']==0
    with db.connect() as c:assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0]==0

def test_quota_pause_retains_progress_and_config(client):
    id,job,payload,parsed,proposal,pub=setup(client)
    with patch.object(batch.providers,'ocr',return_value=parsed),patch.object(batch.models,'generate_text',side_effect=ProviderError('等待额度','waiting_quota')):
        with pytest.raises(ProviderError):batch.run_batch(job['id'],payload)
    with db.connect() as c:
        from backend import models
        assert models.busy(c)
        c.execute("UPDATE jobs SET state='waiting_quota' WHERE id=?",(job['id'],))
    p=client.get('/api/jobs').json()[0]['progress']
    assert p['eta_seconds'] is None
    with patch.object(batch.providers,'ocr',return_value=parsed),patch.object(batch.models,'generate_text',return_value={'text':db.dump(proposal)}):
        assert batch.run_batch(job['id'],payload)['updated']==1

def test_account_fans_not_new_follows_or_combined_likes(client):
    id,job,payload,parsed,proposal,pub=setup(client)
    parsed={'blocks':[{'text':t,'confidence':99} for t in ['粉丝','76','新增粉丝','4','获赞与收藏','610']]}
    proposal['records']=[{'kind':'account','scope':'cumulative','metrics':[
        {'key':'fans','label_index':0,'value_index':1,'value_text':'76','confidence':.99},
        {'key':'fans','label_index':2,'value_index':3,'value_text':'4','confidence':.99},
        {'key':'likes','label_index':4,'value_index':5,'value_text':'610','confidence':.99}]}]
    with patch.object(batch.providers,'ocr',return_value=parsed),patch.object(batch.models,'generate_text',return_value={'text':db.dump(proposal)}):batch.run_batch(job['id'],payload)
    with db.connect() as c:
        assert dict(c.execute('SELECT metric_key,value FROM metric_values').fetchall())=={'fans':76}
        assert c.execute('SELECT count(*) FROM metric_snapshots WHERE publication_id IS NOT NULL').fetchone()[0]==0

def test_known_note_overview_scope_does_not_override_period():
    blocks=[{'text':t} for t in ['笔记分析详情','笔记数据','数据概览']]
    assert batch.resolve_scope({'kind':'post','scope':'unknown'},blocks)=='cumulative'
    assert batch.resolve_scope({'kind':'post','scope':'period'},blocks)=='period'
    assert batch.resolve_scope({'kind':'account','scope':'unknown'},blocks)=='unknown'
    assert batch.resolve_scope({'kind':'post','scope':'unknown'},blocks+[{'text':'近7天'}])=='unknown'

def test_cancelled_batch_does_not_call_ocr(client):
    id,job,payload,parsed,proposal,pub=setup(client)
    with db.connect() as c:c.execute("UPDATE jobs SET state='cancelling' WHERE id=?",(job['id'],))
    with patch.object(batch.providers,'ocr') as ocr:
        with pytest.raises(ProviderError):batch.run_batch(job['id'],payload)
        ocr.assert_not_called()

def test_single_post_uses_explicit_selection_without_title_or_scope_check(client):
    evidence_id,_,_,parsed,proposal,pub=setup(client)
    selected=batch.start_batch({'platform':'xiaohongshu','evidence_ids':[evidence_id],'publication_id':pub['id']})
    with db.connect() as c:
        payload=db.load(db.one(c,'SELECT payload FROM jobs WHERE id=?',(selected['id'],))['payload'])
    assert payload['publication_id']==pub['id']
    with pytest.raises(ValueError,match='所选作品不存在'):
        batch.start_batch({'platform':'douyin','evidence_ids':[evidence_id],'publication_id':pub['id']})
    titleless={**proposal,'records':[{**proposal['records'][0],'title_indices':[],'scope':'period'}]}
    outcome=batch.apply_proposal(evidence_id,'xiaohongshu',titleless,parsed,pub['id'])
    assert outcome['updated'][0]['publication_id']==pub['id']
    with db.connect() as c:
        assert db.one(c,'SELECT source FROM metric_snapshots')['source']=='screenshot_selected_post'
        assert dict(c.execute('SELECT metric_key,value FROM metric_values').fetchall())=={'views':123,'likes':12}
    account_proposal={'records':[{'kind':'account','scope':'cumulative','metrics':[{'key':'fans','label_index':0,'value_index':1,'value_text':'76','confidence':.99}]}]}
    account_image={'blocks':[{'text':'粉丝','confidence':99},{'text':'76','confidence':99}]}
    assert not batch.apply_proposal(evidence_id,'xiaohongshu',account_proposal,account_image,pub['id'])['updated']
    with db.connect() as c:assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0]==1
    again=batch.apply_proposal(evidence_id,'xiaohongshu',titleless,parsed,pub['id'])
    assert again['updated'][0]['publication_id']==pub['id']
    with db.connect() as c:assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0]==1

def test_selected_post_accepts_post_metrics_from_account_classification_but_not_account_totals(client):
    evidence_id,_,_,_,_,pub=setup(client)
    parsed={'blocks':[{'text':t,'confidence':99} for t in ['阅读','57','粉丝总数','76']]}
    proposal={'records':[{'kind':'account','title_indices':[],'scope':'period','metrics':[
        {'key':'views','label_index':0,'value_index':1,'value_text':'57','confidence':.99},
        {'key':'fans','label_index':2,'value_index':3,'value_text':'76','confidence':.99}]}]}
    outcome=batch.apply_proposal(evidence_id,'xiaohongshu',proposal,parsed,pub['id'])
    assert len(outcome['updated'])==1
    with db.connect() as c:
        assert dict(c.execute('SELECT metric_key,value FROM metric_values').fetchall())=={'views':57}
        assert c.execute('SELECT count(*) FROM metric_snapshots WHERE account_id IS NOT NULL').fetchone()[0]==0

def test_selected_post_retry_reuses_cached_ocr_and_proposal(client):
    evidence_id,_,_,parsed,proposal,pub=setup(client)
    proposal['records'][0]['title_indices']=[]
    proposal['records'][0]['scope']='period'
    with db.connect() as c:
        c.execute('UPDATE evidence SET parser_json=? WHERE id=?',(db.dump(parsed),evidence_id))
        c.execute('INSERT INTO screenshot_updates VALUES(?,?,?,NULL,?)',(evidence_id,'xiaohongshu',db.dump(proposal),db.now()))
    selected=batch.start_batch({'platform':'xiaohongshu','evidence_ids':[evidence_id],'publication_id':pub['id']})
    with db.connect() as c:
        payload=db.load(db.one(c,'SELECT payload FROM jobs WHERE id=?',(selected['id'],))['payload'])
    with (patch.object(batch.providers,'tencent_request',side_effect=AssertionError('OCR must not be billed again')),
          patch.object(batch.models,'generate_text',side_effect=AssertionError('model must not run again'))):
        result=batch.run_batch(selected['id'],payload)
    assert result['updated']==1 and result['attention']==0
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0]==1

def test_selected_post_overlapping_values_follow_upload_order(client):
    older,_,_,_,_,pub=setup(client)
    newer=db.uid()
    with db.connect() as c:
        c.execute('UPDATE evidence SET received_at=? WHERE id=?',('2026-09-23T06:00:00+00:00',older))
        c.execute('INSERT INTO evidence(id,kind,relative_path,sha256,received_at) VALUES(?,?,?,?,?)',
                  (newer,'screenshot','evidence/test-newer.png','test-newer-sha','2026-09-23T07:00:00+00:00'))
    def proposal(value):
        parsed={'blocks':[{'text':t,'confidence':99} for t in ['观看',str(value)]]}
        record={'records':[{'kind':'post','title_indices':[],'scope':'period','metrics':[
            {'key':'views','label_index':0,'value_index':1,'value_text':str(value),'confidence':.99}]}]}
        return record,parsed
    latest,parsed=proposal(200)
    assert batch.apply_proposal(newer,'xiaohongshu',latest,parsed,pub['id'])['updated']
    earlier,parsed=proposal(100)
    assert batch.apply_proposal(older,'xiaohongshu',earlier,parsed,pub['id'])['updated']
    publications=client.get('/api/publications').json()
    assert publications[0]['metrics']['values']['views']==200
    job=batch.start_batch({'platform':'xiaohongshu','evidence_ids':[newer,older],'publication_id':pub['id']})
    with db.connect() as c:
        payload=db.load(db.one(c,'SELECT payload FROM jobs WHERE id=?',(job['id'],))['payload'])
    assert payload['evidence_ids']==[older,newer]

def test_reset_clears_upload_inbox_but_preserves_evidence_and_allows_reupload(client):
    raw=png();evidence_id=client.post('/api/updates/screenshots',files={'file':('a.png',raw)}).json()['id']
    assert len(client.get('/api/evidence').json())==1
    result=client.post('/api/updates/screenshots/clear',json={})
    assert result.status_code==200 and result.json()['cleared']==1
    assert client.get('/api/evidence').json()==[]
    assert client.get('/api/evidence/'+evidence_id+'/image').status_code==200
    again=client.post('/api/updates/screenshots',files={'file':('a.png',raw)}).json()
    assert again['duplicate'] and again['id']==evidence_id
    assert len(client.get('/api/evidence').json())==1

def test_reset_refuses_while_screenshot_batch_is_running(client):
    setup(client)
    assert client.post('/api/updates/screenshots/clear',json={}).status_code==409
