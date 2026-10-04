from types import SimpleNamespace
from unittest.mock import patch
import pytest

from backend import db,job_progress,worker,models
from backend.providers import ProviderError
from test_workspace import client


def test_generation_progress_is_durable_and_visible(client):
    worker.worker.stop()
    job=worker.enqueue('plan',{'thoughts':'测试进度'})
    job_progress.start(job['id'],'plan','正在准备创作')
    job_progress.update(job['id'],'drafting',47,'正在生成标题与正文')
    with db.connect() as c:
        c.execute("UPDATE jobs SET state='running' WHERE id=?",(job['id'],))
    visible=next(x for x in client.get('/api/jobs').json() if x['id']==job['id'])
    assert visible['progress']['phase']=='drafting'
    assert visible['progress']['percent']==47
    assert visible['progress']['message']=='正在生成标题与正文'
    assert visible['progress']['estimated_total_seconds']>0


def test_generation_progress_marks_quota_stop_with_retry_time(client):
    worker.worker.stop()
    job=worker.enqueue('review',{'kind':'single','publication_ids':['fixture']})
    job_progress.start(job['id'],'review')
    job_progress.update(job['id'],'model',38,'正在理解复盘上下文')
    retry_at='2026-10-02T04:26:00+00:00'
    with db.connect() as c:
        c.execute("UPDATE jobs SET state='waiting_quota',error=?,next_retry_at=? WHERE id=?",('额度不足',retry_at,job['id']))
    job_progress.stop(job['id'],'waiting_quota','本次模型调用已停止')
    visible=next(x for x in client.get('/api/jobs').json() if x['id']==job['id'])
    assert visible['state']=='waiting_quota'
    assert visible['progress']['phase']=='waiting_quota'
    assert visible['next_retry_at']==retry_at


def test_codex_quota_is_checked_before_starting_a_turn():
    class Dump:
        def __init__(self,value):self.value=value
        def model_dump(self,**kwargs):return self.value
    class FakeClient:
        thread_started=False
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def initialize(self):pass
        def account_read(self):return Dump({'account':{'type':'chatgpt'}})
        def request(self,method,payload,response_model=None):
            if method=='config/read':
                disabled={name:False for name in ['shell_tool','unified_exec','apps','plugins','multi_agent','js_repl','computer_use','browser_use','view_image']}
                return Dump({'config':{'default_permissions':'xiangyang_analysis','permissions':{'xiangyang_analysis':{'filesystem':{':root':'deny',':workspace_roots':'read'},'network':{'enabled':False}}},'features':disabled,'mcp_servers':{}}})
            return Dump({'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':100,'resetsAt':4102444800},'secondary':{'usedPercent':20,'resetsAt':4102444800}}}})
        def thread_start(self,*args,**kwargs):
            self.thread_started=True
            raise AssertionError('quota preflight must prevent turn start')
    with patch('openai_codex.client.CodexClient',FakeClient),patch.object(models,'sdk_config',return_value=SimpleNamespace(cwd='fixture')):
        with pytest.raises(ProviderError) as caught:
            models.generate_codex({'model_id':None},'test')
    assert caught.value.state=='waiting_quota'
