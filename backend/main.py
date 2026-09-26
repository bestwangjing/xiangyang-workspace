import hashlib
import hmac
import io
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from PIL import Image
from . import db

SESSION=secrets.token_urlsafe(32)
WEB=Path(__file__).resolve().parent.parent/'web-dist'

def fail(message, status=400, code='invalid_input'): raise HTTPException(status,dict(code=code,message=message,field_errors={},retryable=False))

@asynccontextmanager
async def lifespan(app):
    db.initialize()
    from .worker import worker
    from .hot_topics import scheduler
    worker.start()
    scheduler.start()
    yield
    scheduler.stop()
    worker.stop()
    from .remote_upload import tunnel
    tunnel.stop()

app=FastAPI(title='向阳AI工作台',lifespan=lifespan,docs_url=None,redoc_url=None)

@app.middleware('http')
async def local_security(request:Request,call_next):
    host=request.headers.get('host','').split(':')[0]
    if host not in ['127.0.0.1','localhost','testserver']: return JSONResponse({'message':'仅允许本机管理访问'},403)
    if request.url.path.startswith('/api/'):
        if request.url.path=='/api/updates/screenshots':
            length=request.headers.get('content-length','0')
            if not length.isdigit() or int(length)>21*1024*1024: return JSONResponse({'message':'截图请求超过大小上限'},413)
        origin=request.headers.get('origin')
        if origin and urlparse(origin).netloc!=request.headers.get('host'): return JSONResponse({'message':'来源不匹配'},403)
        if request.url.path!='/api/session' and not hmac.compare_digest(request.cookies.get('xy_session',''),SESSION): return JSONResponse({'message':'会话已失效，请刷新页面'},401)
        if request.method not in ['GET','HEAD'] and not hmac.compare_digest(request.headers.get('x-csrf-token',''),SESSION): return JSONResponse({'message':'会话校验失败'},403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Cache-Control']='no-store' if request.url.path.startswith('/api') else 'no-cache'
    return response

@app.exception_handler(HTTPException)
async def http_error(request,exc): return JSONResponse(exc.detail if isinstance(exc.detail,dict) else dict(message=str(exc.detail)),exc.status_code)
@app.exception_handler(ValueError)
async def value_error(request,exc): return JSONResponse(dict(code='invalid_input',message=str(exc),retryable=False),400)
@app.exception_handler(sqlite3.Error)
async def storage_error(request,exc): return JSONResponse(dict(code='storage_error',message='数据库写入失败，修改尚未保存。请检查数据盘与剩余空间。',retryable=True),503)

@app.get('/api/session')
def session():
    r=JSONResponse(dict(csrf=SESSION,app='xiangyang-workspace'))
    r.set_cookie('xy_session',SESSION,httponly=True,samesite='strict')
    return r

@app.get('/api/health')
def health():
    with db.connect() as c: c.execute('SELECT 1').fetchone()
    return dict(ok=True,app='xiangyang-workspace',database=str(db.path()))

@app.get('/api/storage')
def storage():
    import shutil
    usage=shutil.disk_usage(db.ROOT)
    with db.connect() as c:
        return dict(root=str(db.ROOT),database=str(db.path()),size=db.path().stat().st_size,free=usage.free,contents=c.execute('SELECT count(*) FROM contents').fetchone()[0],media=c.execute('SELECT count(*) FROM media_assets').fetchone()[0])

@app.get('/api/profile')
def profile():
    with db.connect() as c:
        result=db.snapshot(c)
        result['homepages']={a['platform']:a['homepage'] for a in db.rows(c,'SELECT platform,homepage FROM accounts')}
        return result

class ProfileInput(BaseModel):
    expected_profile_version:int
    expected_topic_rule_version:int
    xiaohongshu_name:str=Field(min_length=1,max_length=80)
    douyin_name:str=Field(min_length=1,max_length=80)
    xiaohongshu_homepage:str=Field(default='',max_length=300)
    douyin_homepage:str=Field(default='',max_length=300)
    position:str=Field(min_length=1,max_length=600)
    audience:str=Field(min_length=1,max_length=600)
    promise:str=Field(default='',max_length=600)
    primary_goal:Literal['涨粉','个人品牌','获客','变现']
    secondary_goal:Literal['涨粉','个人品牌','获客','变现']
    formats:list[Literal['image_post','video']]=Field(min_length=1,max_length=2)
    weekly_target:int=Field(ge=1,le=30,strict=True)
    style:str=Field(default='',max_length=600)
    avoid:str=Field(default='',max_length=600)
    keywords:list[str]=Field(max_length=20)
    exclusions:list[str]=Field(max_length=100)
    @model_validator(mode='after')
    def check(self):
        if self.primary_goal==self.secondary_goal: raise ValueError('优先目标与次要目标不能相同')
        for field in ['xiaohongshu_name','douyin_name','position','audience']:
            if not getattr(self,field).strip(): raise ValueError('必填字段不能是空白')
        if any(not x.strip() or len(x)>80 for x in self.keywords+self.exclusions): raise ValueError('关键词不能为空或超过80字')
        link=self.xiaohongshu_homepage.strip()
        if link:
            from .homepage_metrics import parse_homepage,PLATFORM_NAMES
            parsed=parse_homepage(link)
            if parsed['platform']!='xiaohongshu': raise ValueError(PLATFORM_NAMES['xiaohongshu']+'账号应填写'+PLATFORM_NAMES['xiaohongshu']+'个人主页链接')
        dy_link=self.douyin_homepage.strip()
        if dy_link:
            from .homepage_metrics import parse_homepage,PLATFORM_NAMES
            parsed=parse_homepage(dy_link)
            if parsed['platform']!='douyin': raise ValueError(PLATFORM_NAMES['douyin']+'账号应填写'+PLATFORM_NAMES['douyin']+'个人主页链接（www.douyin.com/user/… 或手机 App 分享主页得到的 v.douyin.com 短链）')
        return self

@app.put('/api/profile')
def save_profile(value:ProfileInput):
    payload=value.model_dump(exclude={'expected_profile_version','expected_topic_rule_version','keywords','exclusions','xiaohongshu_homepage','douyin_homepage'})
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old=db.snapshot(c)
        if old['profile_version']!=value.expected_profile_version or old['topic_version']!=value.expected_topic_rule_version: fail('配置已在其他窗口更新，请保留编辑内容并重新加载。',409,'version_conflict')
        version=old['profile_version']+1
        topic_version=old['topic_version']
        c.execute('INSERT INTO profile_versions VALUES(?,?,?)',(version,db.dump(payload),db.now()))
        if old['rules']['keywords']!=value.keywords or old['rules']['exclusions']!=value.exclusions:
            topic_version+=1
            rules={**old['rules'],'keywords':value.keywords,'exclusions':value.exclusions}
            c.execute("INSERT INTO rule_versions(kind,version,payload,created_at) VALUES('topic',?,?,?)",(topic_version,db.dump(rules),db.now()))
        c.execute('UPDATE workspace_settings SET profile_version=?,topic_version=? WHERE id=1',(version,topic_version))
        for platform in ['xiaohongshu','douyin']:
            c.execute('UPDATE accounts SET nickname=? WHERE platform=?',(payload[platform+'_name'],platform))
        homepage=value.xiaohongshu_homepage.strip() or None
        if homepage:
            from .homepage_metrics import parse_homepage
            homepage=parse_homepage(homepage)['url']
        c.execute("UPDATE accounts SET homepage=? WHERE platform='xiaohongshu'",(homepage,))
        dy_homepage=value.douyin_homepage.strip() or None
        if dy_homepage:
            from .homepage_metrics import parse_homepage
            dy_homepage=parse_homepage(dy_homepage)['url']
        c.execute("UPDATE accounts SET homepage=? WHERE platform='douyin'",(dy_homepage,))
        db.audit(c,'profile_saved',str(version))
        result=db.snapshot(c)
        result['homepages']={a['platform']:a['homepage'] for a in db.rows(c,'SELECT platform,homepage FROM accounts')}
        return result

@app.get('/api/rules/{kind}')
def get_rules(kind:str):
    if kind not in ['topic','review']: fail('未知规则')
    with db.connect() as c:
        row=db.one(c,'SELECT * FROM rule_versions WHERE kind=? ORDER BY version DESC LIMIT 1',(kind,))
        return dict(version=row['version'],payload=db.load(row['payload']))

@app.put('/api/rules/{kind}')
def save_rules(kind:str,value:dict):
    if kind not in ['topic','review']: fail('未知规则')
    payload=value.get('payload',{})
    payload.pop('_version',None)
    if kind=='topic':
        weights=payload.get('weights',{})
        if set(weights)!=set(db.TOPIC['weights']) or any(type(v) not in [int,float] or not __import__('math').isfinite(v) or v<0 or v>100 for v in weights.values()) or abs(sum(weights.values())-100)>0.0001: fail('七维权重必须合计100%')
        if not isinstance(payload.get('platforms'),list) or any(x not in ['douyin','xiaohongshu','zhihu','bilibili'] for x in payload['platforms']): fail('平台开关无效')
        for key in ['keywords','exclusions']:
            if not isinstance(payload.get(key),list) or any(not isinstance(x,str) or not x.strip() or len(x)>80 for x in payload[key]): fail('关键词格式无效')
        if len(payload['keywords'])>20: fail('关注领域最多配置20个')
    else:
        if any(type(payload.get(k)) is not int for k in ['min_observation','min_sample']) or not 3<=payload.get('min_observation',0)<=payload.get('min_sample',0)<=100: fail('样本提示阈值无效')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT max(version) FROM rule_versions WHERE kind=?',(kind,)).fetchone()[0]
        if old!=value.get('expected_version'): fail('规则已更新，请重新加载',409)
        c.execute('INSERT INTO rule_versions(kind,version,payload,created_at) VALUES(?,?,?,?)',(kind,old+1,db.dump(payload),db.now()))
        column='topic_version' if kind=='topic' else 'review_version'
        c.execute(f'UPDATE workspace_settings SET {column}=? WHERE id=1',(old+1,))
        db.audit(c,'rules_saved',kind)
    return dict(version=old+1,payload=payload)

@app.get('/api/publications')
def publications():
    with db.connect() as c:
        result=db.rows(c,'''SELECT p.*,COALESCE(o.title,p.title_override,r.title) title,r.body_text,COALESCE(p.platform_kind,c.kind) kind
            FROM publications p
            LEFT JOIN contents c ON c.id=p.content_id
            LEFT JOIN content_revisions r ON r.content_id=c.id AND r.revision=c.revision
            LEFT JOIN publication_overrides o ON o.publication_id=p.id
            ORDER BY p.published_local_date DESC''')
        for item in result:
            item['metrics']=latest_metrics(c,'publication_id',item['id'])
            item['media']=db.rows(c,'SELECT id,mime,role,availability FROM media_assets WHERE content_id=? AND availability="ready" ORDER BY sort_order',(item['content_id'],)) if item['content_id'] else []
            override=db.one(c,'SELECT * FROM publication_overrides WHERE publication_id=?',(item['id'],)) or {}
            item['override']=override
            item['paid_status']=override.get('paid_status','unknown')
            for key in ['title','body_text']:
                if override.get(key): item[key]=override[key]
            if override.get('cover_asset_id'):
                item['media']=[{**m,'role':'cover' if m['id']==override['cover_asset_id'] else ('image' if m['role']=='cover' else m['role'])} for m in item['media']]
        return result

@app.patch('/api/publications/{publication_id}')
def edit_publication(publication_id:str,value:dict):
    if value.get('paid_status','unknown') not in ['unknown','organic','paid']: fail('投流状态无效')
    for key,limit in [('title',300),('body_text',100000)]:
        if value.get(key) is not None and (not isinstance(value[key],str) or len(value[key])>limit): fail('平台文案格式或长度无效')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p=db.one(c,'SELECT * FROM publications WHERE id=?',(publication_id,))
        if not p: fail('发布记录不存在',404)
        if p['version']!=value.get('expected_version'): fail('发布记录已修改，请刷新',409)
        cover=value.get('cover_asset_id') or None
        if cover and (not p['content_id'] or not db.one(c,"SELECT id FROM media_assets WHERE id=? AND content_id=? AND mime LIKE 'image/%'",(cover,p['content_id']))): fail('封面必须是本母版的图片')
        c.execute('INSERT INTO publication_overrides VALUES(?,?,?,?,?) ON CONFLICT(publication_id) DO UPDATE SET title=excluded.title,body_text=excluded.body_text,cover_asset_id=excluded.cover_asset_id,paid_status=excluded.paid_status',(publication_id,value.get('title') or None,value.get('body_text') or None,cover,value.get('paid_status','unknown')))
        c.execute('UPDATE publications SET version=version+1 WHERE id=?',(publication_id,))
        db.audit(c,'publication_edited',publication_id)
    return dict(ok=True)

def latest_metrics(c,column,subject):
    # Preserve unknown timestamps; never treat upload time as observation time.
    rows=db.rows(c,f"""SELECT s.id,s.observed_at,s.scope,s.source FROM metric_snapshots s
        LEFT JOIN evidence e ON e.id=s.evidence_id
        WHERE s.{column}=? AND s.scope='cumulative'
        ORDER BY julianday(s.observed_at) DESC,
        CASE WHEN s.source='screenshot_selected_post' THEN julianday(e.received_at) ELSE julianday(s.created_at) END DESC,
        s.created_at DESC""",(subject,))
    from .metrics import effective_values
    values={};sources={}
    seen=set()
    for row in rows:
        if row['id'] in seen: continue
        seen.add(row['id'])
        for key,value in effective_values(c,row['id']).items():
            if key not in values:
                values[key]=value;sources[key]=dict(snapshot_id=row['id'],observed_at=row['observed_at'],source=row['source'])
    return dict(values=values,field_sources=sources,observed_at=rows[0]['observed_at'] if rows else None)

@app.get('/api/dashboard')
def dashboard(days:int=7):
    if days not in [7,30]: fail('日期范围无效')
    local_tz=timezone(timedelta(hours=8))
    end=datetime.now(local_tz).date()
    start=end-timedelta(days=days)
    with db.connect() as c:
        accounts=db.rows(c,'SELECT * FROM accounts')
        for a in accounts:
            a['metrics']=latest_metrics(c,'account_id',a['id'])
            a['local_count']=c.execute("SELECT count(*) FROM publications WHERE account_id=? AND status='published'",(a['id'],)).fetchone()[0]
            a['period_count']=c.execute("SELECT count(*) FROM publications WHERE account_id=? AND status='published' AND published_time_status='confirmed' AND published_local_date>? AND published_local_date<=?",(a['id'],start.isoformat(),end.isoformat())).fetchone()[0]
            a['unknown_dates']=c.execute("SELECT count(*) FROM publications WHERE account_id=? AND status='published' AND published_time_status!='confirmed'",(a['id'],)).fetchone()[0]
            from .metrics import effective_values
            points=[dict(observed_at=x['observed_at'],value=effective_values(c,x['id']).get('fans')) for x in db.rows(c,"SELECT id,observed_at FROM metric_snapshots WHERE account_id=? AND scope='cumulative' AND observed_at IS NOT NULL ORDER BY observed_at",(a['id'],))]
            a['points']=points
            for point in points: point['local_date']=datetime.fromisoformat(point['observed_at']).astimezone(local_tz).date().isoformat()
            first=next((x for x in reversed(points) if x['local_date']==start.isoformat()),None)
            last=next((x for x in reversed(points) if x['local_date']==end.isoformat()),None)
            a['growth']=last['value']-first['value'] if first and last and first['value'] is not None and last['value'] is not None else None
        report=db.one(c,"SELECT report_json,created_at FROM review_reports WHERE kind='account' ORDER BY created_at DESC LIMIT 1")
        analysis=dict(**db.load(report['report_json']),created_at=report['created_at']) if report else None
        return dict(accounts=accounts,start=str(start),end=str(end),profile=db.snapshot(c),analysis=analysis,local_contents=c.execute('SELECT count(*) FROM contents').fetchone()[0])

@app.get('/api/jobs')
def jobs():
    with db.connect() as c:
        result=db.rows(c,'SELECT id,type,state,result,error,attempts,next_retry_at,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT 50')
        for item in result:
            item['result']=db.load(item['result']) if item['result'] else None
            if item['type']=='screenshot_batch':
                from .screenshot_batch import progress
                item['progress']=progress(c,item['id'])
        return result

@app.post('/api/jobs/{job_id}/retry')
def retry_job(job_id:str):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job=db.one(c,'SELECT * FROM jobs WHERE id=?',(job_id,))
        if not job: fail('任务不存在',404)
        if job['state'] not in ['failed','partial','interrupted','waiting_auth','waiting_quota','blocked_config','needs_model_selection']: fail('当前状态不能自动重试；远端执行不明时需先核实。',409)
        if job['config_version_id']:
            active=db.one(c,'SELECT * FROM active_model')
            if active['config_version_id']!=job['config_version_id']: fail('请恢复任务原模型配置后重试',409)
        c.execute("UPDATE jobs SET state='queued',error=NULL,updated_at=? WHERE id=?",(db.now(),job_id))
    return dict(ok=True)

@app.post('/api/jobs/{job_id}/resolve')
def resolve_job(job_id:str,value:dict):
    if value.get('remote_terminated') is not True or not str(value.get('reason','')).strip(): fail('请先核实远端已终止，并填写核实说明')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job=db.one(c,'SELECT * FROM jobs WHERE id=?',(job_id,))
        if not job or job['state']!='recovery_required': fail('任务不处于待核实状态',409)
        c.execute("UPDATE jobs SET state='failed',error=?,updated_at=? WHERE id=?",('已人工核实远端终止：'+value['reason'][:400],db.now(),job_id))
        db.audit(c,'remote_termination_confirmed',job_id)
    return dict(ok=True)

@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id:str):
    from .models import interrupt_job
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        job=db.one(c,'SELECT * FROM jobs WHERE id=?',(job_id,))
        if not job: fail('任务不存在',404)
        if job['state'] in ['queued','waiting_auth','waiting_quota','blocked_config','needs_model_selection']: state='cancelled'
        elif job['state']=='running' and job['type'] in ['review','plan','topics','model_test','trend_relevance','screenshot_batch']:state='cancelling'
        else:fail('当前任务不支持直接取消；远端状态不明时请先核实',409)
        c.execute('UPDATE jobs SET state=?,updated_at=? WHERE id=?',(state,db.now(),job_id))
        db.audit(c,'cancel_requested',job_id)
    if state=='cancelling':interrupt_job(job_id)
    return dict(state=state)

@app.get('/api/media/{asset_id}')
def media(asset_id:str):
    with db.connect() as c: asset=db.one(c,'SELECT * FROM media_assets WHERE id=?',(asset_id,))
    if not asset: fail('素材不存在',404)
    path=db.confined(asset['relative_path'])
    if not path.is_file(): fail('本地素材缺失，请补拷后重新同步',404,'missing_media')
    return FileResponse(path,media_type=asset['mime'])

def store_evidence(raw:bytes):
    if len(raw)>20*1024*1024: fail('截图不能超过20MB')
    try:
        im=Image.open(io.BytesIO(raw))
        if im.format not in ['PNG','JPEG','WEBP'] or im.width*im.height>40_000_000: raise ValueError()
        fmt=im.format
        im.verify()
    except Exception: fail('请选择有效的 PNG、JPEG 或 WebP 截图')
    hashed=hashlib.sha256(raw).hexdigest()
    with db.connect() as c:
        old=db.one(c,'SELECT id FROM evidence WHERE sha256=?',(hashed,))
        if old:
            c.execute('UPDATE evidence SET inbox_cleared_at=NULL,received_at=? WHERE id=? AND inbox_cleared_at IS NOT NULL',(db.now(),old['id']))
            return dict(id=old['id'],duplicate=True)
    evidence_id=db.uid()
    rel=f"evidence/{evidence_id}/original.{dict(PNG='png',JPEG='jpg',WEBP='webp')[fmt]}"
    path=db.confined(rel)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(raw)
    with db.connect() as c:
        c.execute('INSERT INTO evidence(id,kind,relative_path,sha256,received_at) VALUES(?,?,?,?,?)',(evidence_id,'screenshot',rel,hashed,db.now()))
    return dict(id=evidence_id,duplicate=False)

@app.post('/api/updates/screenshots')
async def upload(file:UploadFile=File(...)):
    return store_evidence(await file.read(20*1024*1024+1))

@app.post('/api/updates/screenshots/clear')
def clear_screenshot_inbox():
    """Clear the update inbox while retaining evidence used by audit/history."""
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        active=c.execute("SELECT 1 FROM jobs WHERE type='screenshot_batch' AND state IN ('queued','running','cancelling','waiting_quota','waiting_auth','recovery_required','blocked_config','needs_model_selection') LIMIT 1").fetchone()
        if active: fail('截图更新任务未结束，请在任务记录中处理后再清空',409)
        count=c.execute("SELECT count(*) FROM evidence WHERE kind='screenshot' AND inbox_cleared_at IS NULL").fetchone()[0]
        c.execute("UPDATE evidence SET inbox_cleared_at=? WHERE kind='screenshot' AND inbox_cleared_at IS NULL",(db.now(),))
        db.audit(c,'screenshot_inbox_cleared',str(count))
    return dict(cleared=count,evidence_retained=True)

@app.post('/api/updates/batch')
def screenshot_batch(value:dict):
    from .screenshot_batch import start_batch
    return start_batch(value)

@app.get('/api/evidence')
def evidence_list():
    with db.connect() as c:
        result=db.rows(c,"SELECT id,received_at,parser_json FROM evidence WHERE kind='screenshot' AND inbox_cleared_at IS NULL ORDER BY received_at DESC")
        for x in result:
            x['parsed']=db.load(x.pop('parser_json')) if x['parser_json'] else None
            update=db.one(c,'SELECT platform,outcome FROM screenshot_updates WHERE evidence_id=?',(x['id'],))
            x['updated_platform']=update['platform'] if update and update['outcome'] else None
        return result

@app.get('/api/evidence/{evidence_id}/image')
def evidence_image(evidence_id:str):
    with db.connect() as c: item=db.one(c,'SELECT relative_path FROM evidence WHERE id=?',(evidence_id,))
    if not item: fail('截图不存在',404)
    return FileResponse(db.confined(item['relative_path']))

@app.post('/api/evidence/{evidence_id}/ocr')
def start_ocr(evidence_id:str,value:dict|None=None):
    from .worker import enqueue
    method=(value or {}).get('method','GeneralBasicOCR')
    if method not in ['GeneralBasicOCR','GeneralAccurateOCR']:fail('OCR方法无效')
    return enqueue('ocr',dict(evidence_id=evidence_id,method=method),'ocr:'+evidence_id+':'+method)

@app.post('/api/evidence/{evidence_id}/confirm')
def confirm_evidence(evidence_id:str,value:dict):
    from .metrics import confirm
    return confirm(evidence_id,value)

@app.post('/api/publications/{publication_id}/publication-time/confirm')
def confirm_publication_time(publication_id:str,value:dict):
    from datetime import date
    evidence_id=value.get('evidence_id')
    precision=value.get('precision','day')
    if precision not in ['day','minute','second']: fail('时间精度无效')
    instant=None
    local_date=date.fromisoformat(value.get('local_date','')).isoformat()
    if precision!='day':
        dt=datetime.fromisoformat(value.get('instant',''))
        if dt.tzinfo is None: fail('精确发布时间必须有时区')
        if dt.date().isoformat()!=local_date: fail('日期与精确时间不一致')
        instant=dt.isoformat()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p=db.one(c,'SELECT * FROM publications WHERE id=?',(publication_id,))
        if not p: fail('发布记录不存在',404)
        if p['version']!=value.get('expected_version'): fail('记录已变化，请刷新',409)
        if not db.one(c,"SELECT id FROM evidence WHERE id=? AND kind='screenshot'",(evidence_id,)): fail('必须关联已上传的原始后台截图')
        if p['published_time_status']=='confirmed' and (p['published_local_date']!=local_date or p['published_at']!=instant) and not value.get('reason'): fail('时间冲突，请填写核对后的修正原因')
        old=dict(date=p['published_local_date'],instant=p['published_at'],precision=p['published_precision'])
        new=dict(date=local_date,instant=instant,precision=precision)
        c.execute('INSERT INTO publication_time_history VALUES(?,?,?,?,?,?,?)',(db.uid(),publication_id,evidence_id,db.dump(old),db.dump(new),value.get('reason','后台截图人工核对'),db.now()))
        c.execute("UPDATE publications SET published_local_date=?,published_at=?,published_precision=?,published_time_status='confirmed',published_evidence_id=?,version=version+1 WHERE id=?",(local_date,instant,precision,evidence_id,publication_id))
    return dict(ok=True)

@app.get('/api/publications/{publication_id}/publication-time/evidence')
def publication_time_evidence(publication_id:str):
    with db.connect() as c: return db.rows(c,'SELECT * FROM publication_time_history WHERE publication_id=? ORDER BY created_at',(publication_id,))

@app.post('/api/storage/backup')
def backup():
    id=db.uid(); dest=db.ROOT/'backups'/f'workspace-{id}.db'
    with db.connect() as c, sqlite3.connect(dest) as target: c.backup(target)
    return dict(filename=dest.name,includes_media=False,message='数据库备份完成；媒体文件需单独备份。')

from . import providers, models, reports, mobile, homepage_metrics, douyin_login, platform_posts
app.include_router(providers.router)
app.include_router(models.router)
app.include_router(reports.router)
app.include_router(homepage_metrics.router)
app.include_router(douyin_login.router)
app.include_router(platform_posts.router)
app.include_router(mobile.router)

if WEB.exists(): app.mount('/',StaticFiles(directory=WEB,html=True),name='web')
