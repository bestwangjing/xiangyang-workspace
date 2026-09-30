import threading
from . import db,sync

GENERATIONS=('review','plan','topics','experience','model_test','trend_relevance','screenshot_batch')

def enqueue(kind,payload,key=None,exclusive=False):
    payload={k:v for k,v in payload.items() if k!='idempotency_key'}
    key=key or db.uid(); hashed=sync.digest(payload)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if exclusive:
            running=db.one(c,"SELECT id,state FROM jobs WHERE type=? AND state IN ('queued','running','waiting_auth','waiting_quota','recovery_required') ORDER BY created_at DESC LIMIT 1",(kind,))
            if running:return dict(**running,busy=True)
        old=db.one(c,'SELECT id,payload_hash,state FROM jobs WHERE request_key=?',(key,))
        if old:
            if old['payload_hash']!=hashed: raise ValueError('相同请求标识不能用于不同输入')
            return dict(id=old['id'],state=old['state'])
        active=db.one(c,'SELECT * FROM active_model') if kind in GENERATIONS else None
        if kind in GENERATIONS:
            payload['snapshot']=db.snapshot(c)
            payload['config_version_id']=active['config_version_id']
        id=db.uid()
        c.execute('INSERT INTO jobs(id,type,request_key,payload_hash,payload,state,config_version_id,active_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(id,kind,key,hashed,db.dump(payload),'queued',active['config_version_id'] if active else None,active['revision'] if active else None,db.now(),db.now()))
        return dict(id=id,state='queued')

class Worker:
    def __init__(self): self.event=threading.Event(); self.thread=None
    def start(self):
        self.event.clear()
        self.thread=threading.Thread(target=self.loop,daemon=True,name='workspace-worker'); self.thread.start()
    def stop(self):
        self.event.set()
        if self.thread and self.thread.is_alive(): self.thread.join(timeout=2)
    def loop(self):
        while not self.event.wait(.5):
            try:
                with db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute("UPDATE jobs SET state='queued',next_retry_at=NULL WHERE state='waiting_quota' AND next_retry_at IS NOT NULL AND next_retry_at<=? AND attempts<4 AND config_version_id=(SELECT config_version_id FROM active_model WHERE id=1)",(db.now(),))
                    locked=c.execute("SELECT 1 FROM jobs WHERE state IN ('running','recovery_required','cancelling') AND type IN ('review','plan','topics','experience','model_test','trend_relevance','screenshot_batch')").fetchone()
                    job=db.one(c,"SELECT * FROM jobs WHERE state='queued' AND (?=0 OR type NOT IN ('review','plan','topics','experience','model_test','trend_relevance','screenshot_batch')) ORDER BY created_at LIMIT 1",(1 if locked else 0,))
                    if not job: continue
                    if job['type'] in GENERATIONS:
                        unresolved=c.execute("SELECT 1 FROM jobs WHERE state IN ('running','recovery_required','cancelling') AND type IN ('review','plan','topics','experience','model_test','trend_relevance','screenshot_batch')").fetchone()
                        if unresolved: continue
                        active=db.one(c,'SELECT * FROM active_model')
                        if active['config_version_id']!=job['config_version_id']:
                            c.execute("UPDATE jobs SET state='needs_model_selection' WHERE id=?",(job['id'],)); continue
                    c.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?",(db.now(),job['id']))
                try:
                    payload=db.load(job['payload'])
                    if job['type']=='screenshot_batch':
                        from .screenshot_batch import run_batch
                        result=run_batch(job['id'],payload)
                    elif job['type']=='ocr':
                        from .providers import ocr
                        result=ocr(payload['evidence_id'],payload.get('method','GeneralBasicOCR'))
                    elif job['type']=='references':
                        from .providers import fetch_references
                        result=fetch_references(payload)
                    elif job['type']=='trends':
                        from .providers import fetch_trends
                        result=fetch_trends(payload)
                    elif job['type']=='hot_topics':
                        from .hot_topics import collect
                        result=collect(payload)
                    elif job['type']=='posts_fetch':
                        from .platform_posts import run_posts_fetch
                        result=run_posts_fetch(job['id'],payload)
                    elif job['type']=='trend_relevance':
                        from .trend_relevance import analyze
                        result=analyze(job['id'],payload)
                    elif job['type']=='homepage_fetch':
                        from .homepage_metrics import run_homepage_fetch
                        result=run_homepage_fetch(job['id'],payload)
                    elif job['type'] in GENERATIONS:
                        from .reports import generate
                        result=generate(job['id'],job['type'],payload)
                    else: raise ValueError('未知任务类型')
                    state='partial' if (job['type']=='screenshot_batch' and result.get('attention',0)) or (job['type'] in ('hot_topics','homepage_fetch','posts_fetch') and result.get('errors')) else 'completed'; error=None; retry_at=None
                except Exception as exc:
                    result=None
                    # Provider implementations supply safe user-facing error messages only.
                    error=str(exc)[:600]
                    state=getattr(exc,'state','failed')
                    retry_at=getattr(exc,'retry_at',None)
                with db.connect() as c:
                    c.execute("UPDATE jobs SET state=?,result=CASE WHEN ?='recovery_required' THEN result ELSE ? END,error=?,next_retry_at=?,updated_at=? WHERE id=?",(state,state,db.dump(result) if result else None,error,retry_at,db.now(),job['id']))
            except Exception:
                # Storage errors must not cause a busy retry loop or lose the durable running record.
                self.event.wait(3)

worker=Worker()
