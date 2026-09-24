import base64
import ctypes
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter
from . import db,sync
from .metrics import number,parse_ocr

router=APIRouter(prefix='/api')

def numeric_heat(value):
    """Normalize a source heat value only when its unit is unambiguous."""
    if isinstance(value,(int,float)): return float(value)
    raw=str(value or '').strip().replace(',','').lower()
    match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*(w|万|k|千|亿)?',raw)
    if not match:return None
    return float(match.group(1))*{'w':10000,'万':10000,'k':1000,'千':1000,'亿':100000000,None:1}[match.group(2)]

class ProviderError(Exception):
    def __init__(self,message,state='failed',retry_at=None): super().__init__(message); self.state=state; self.retry_at=retry_at

def reserve_call(provider,operation):
    from datetime import datetime,timezone,timedelta
    today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        budget=db.one(c,'SELECT daily_call_limit FROM provider_budgets WHERE provider=?',(provider,))
        limit=budget['daily_call_limit'] if budget else 50
        used=c.execute('SELECT count(*) FROM provider_calls WHERE provider=? AND local_day=?',(provider,today)).fetchone()[0]
        if used>=limit:raise ProviderError('已达到该服务今日调用上限，请在数据源设置调整或明日再试。','blocked_config')
        c.execute('INSERT INTO provider_calls VALUES(?,?,?,?,?)',(db.uid(),provider,operation,db.now(),today))

@router.get('/provider-budgets')
def budgets():
    from datetime import datetime,timezone,timedelta
    today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    with db.connect() as c:
        result=[]
        for provider in ['redfox','tencent-ocr','tikhub']:
            row=db.one(c,'SELECT daily_call_limit FROM provider_budgets WHERE provider=?',(provider,))
            used=c.execute('SELECT count(*) FROM provider_calls WHERE provider=? AND local_day=?',(provider,today)).fetchone()[0]
            result.append(dict(provider=provider,daily_call_limit=row['daily_call_limit'] if row else 50,used=used,day=today,cost=None))
        return result

@router.put('/provider-budgets/{provider}')
def save_budget(provider:str,value:dict):
    limit=value.get('daily_call_limit')
    if provider not in ['redfox','tencent-ocr','tikhub'] or type(limit) is not int or not 0<=limit<=10000:raise ValueError('每日调用上限必须是0至10000的整数')
    with db.connect() as c:c.execute('INSERT INTO provider_budgets VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET daily_call_limit=excluded.daily_call_limit',(provider,limit))
    return dict(ok=True)

class Blob(ctypes.Structure): _fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_byte))]

def protect(data:bytes,decrypt=False):
    if os.name!='nt': raise ValueError('凭据保存需要 Windows DPAPI')
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_byte))); target=Blob()
    call=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not call(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)): raise ValueError('Windows 凭据加解密失败')
    try: return ctypes.string_at(target.data,target.size)
    finally: ctypes.windll.kernel32.LocalFree(target.data)

def secret_root():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'XiangyangWorkspace'/'credentials'

def save_secret(provider,value,max_length=10000):
    if not value or len(value)>max_length: raise ValueError('凭据不能为空或过长')
    id=db.uid(); encrypted=protect(value.encode())
    root=secret_root();root.mkdir(parents=True,exist_ok=True)
    (root/(id+'.bin')).write_bytes(encrypted)
    mask='••••'+value[-4:] if provider!='tencent-ocr' else 'SecretId / SecretKey 已保存'
    with db.connect() as c: c.execute('INSERT INTO credentials VALUES(?,?,?,?,?)',(id,provider,b'',mask,db.now()))
    return id

def get_secret(id):
    if not id: raise ProviderError('请先配置服务凭据','blocked_config')
    file=secret_root()/(str(__import__('uuid').UUID(id))+'.bin')
    if not file.exists(): raise ProviderError('凭据文件缺失，请重新配置','blocked_config')
    return protect(file.read_bytes(),True).decode()

def provider_secret(provider):
    with db.connect() as c: row=db.one(c,'SELECT credential_ref FROM provider_settings WHERE provider=?',(provider,))
    return get_secret(row['credential_ref'] if row else None)

@router.get('/providers')
def provider_status():
    with db.connect() as c:
        result=[]
        for name in ['redfox','tencent-ocr','tikhub']:
            row=db.one(c,'SELECT p.provider,p.status,p.updated_at,c.mask,v.error AS validation_error FROM provider_settings p LEFT JOIN provider_validation v ON v.credential_ref=p.credential_ref LEFT JOIN credentials c ON c.id=p.credential_ref WHERE p.provider=?',(name,))
            result.append(row or dict(provider=name,status='unconfigured',mask=None))
        return result

@router.put('/providers/{provider}/key')
def configure(provider:str,value:dict):
    if provider not in ['redfox','tencent-ocr','tikhub']: raise ValueError('未知服务')
    if provider=='tencent-ocr':
        if not value.get('secret_id') or not value.get('secret_key'): raise ValueError('请填写 SecretId 和 SecretKey')
        secret=db.dump(dict(secret_id=value['secret_id'],secret_key=value['secret_key']))
    else: secret=value.get('key','').strip()
    ref=save_secret(provider,secret)
    with db.connect() as c: c.execute('INSERT INTO provider_settings VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET credential_ref=excluded.credential_ref,status=excluded.status,updated_at=excluded.updated_at',(provider,ref,'saved_unverified',db.now()))
    return validate_configuration(provider,ref)

def tencent_request(secret,image_bytes,method='GeneralBasicOCR'):
    credentials=db.load(secret)
    reserve_call('tencent-ocr',method)
    from tencentcloud.common import credential
    from tencentcloud.ocr.v20181119 import ocr_client,models
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    try:
        http=HttpProfile();http.reqTimeout=30
        profile=ClientProfile();profile.httpProfile=http
        client=ocr_client.OcrClient(credential.Credential(credentials['secret_id'],credentials['secret_key']),'ap-guangzhou',profile)
        req=getattr(models,method+'Request')()
        req.ImageBase64=base64.b64encode(image_bytes).decode()
        return __import__('json').loads(getattr(client,method)(req).to_json_string())
    except Exception as exc:
        code=exc.get_code() if hasattr(exc,'get_code') else type(exc).__name__
        hint='；文字识别服务尚未开通，请在腾讯云 OCR 控制台开通后重新保存配置。' if code=='FailedOperation.UnOpenError' else ''
        raise ProviderError('腾讯 OCR 调用失败：'+str(code)+hint,'blocked_config' if 'Auth' in str(code) or code=='FailedOperation.UnOpenError' else 'failed') from None


def validation_probe(provider,secret):
    if provider=='tencent-ocr':
        import io
        from PIL import Image,ImageDraw,ImageFont
        image=Image.new('RGB',(800,240),'white')
        ImageDraw.Draw(image).text((35,60),'OCR TEST 1234',font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',52),fill='black')
        buf=io.BytesIO();image.save(buf,format='PNG')
        response=tencent_request(secret,buf.getvalue())
        if not any('1234' in x.get('DetectedText','') for x in response.get('TextDetections',[])):
            raise ProviderError('OCR 请求成功，但测试文字未正确识别，请检查识别结果')
    elif provider=='tikhub':
        from .hot_topics import request_douyin_posts
        request_douyin_posts(secret,'AI实战')
    else:
        from datetime import datetime,timedelta
        end=datetime.now();start=end-timedelta(hours=1)
        reserve_call(provider,'configuration_validation')
        response=httpx.post('https://redfox.hk/story/api/hotSpot/getListByPlatformWithKeyword',headers={'X-API-KEY':secret},json=dict(source='全平台热点事件',platforms=[2],keywords=[],startDate=start.strftime('%Y-%m-%d %H:%M:%S'),endDate=end.strftime('%Y-%m-%d %H:%M:%S')),timeout=30,follow_redirects=False)
        if response.status_code!=200:raise ProviderError(f'RedFox 验证失败（HTTP {response.status_code}）')
        data=response.json()
        if data.get('code')!=2000 or not isinstance(data.get('data'),dict) or not isinstance(data['data'].get('dyList'),list):
            raise ProviderError('RedFox 验证未通过，请检查权限及额度')


def validate_configuration(provider,ref):
    # Each save validates its immutable credential; older responses cannot certify a newer key.
    with db.connect() as c:
        changed=c.execute("UPDATE provider_settings SET status='verifying' WHERE provider=? AND credential_ref=?",(provider,ref)).rowcount
    if not changed:return dict(status='superseded')
    error=None;status='verified'
    try:validation_probe(provider,get_secret(ref))
    except ProviderError as exc:
        error=str(exc);status='blocked_auth' if 'Auth' in error else 'failed'
    except Exception as exc:
        error='验证未完成（'+type(exc).__name__+'），凭据已保存；未自动重试。';status='failed'
    with db.connect() as c:
        c.execute('INSERT OR REPLACE INTO provider_validation VALUES(?,?,?,?)',(ref,status,error,db.now()))
        changed=c.execute('UPDATE provider_settings SET status=?,updated_at=? WHERE provider=? AND credential_ref=?',(status,db.now(),provider,ref)).rowcount
    return dict(status=status if changed else 'superseded',validation_error=error)


@router.delete('/providers/{provider}/key')
def remove_key(provider:str):
    with db.connect() as c: c.execute("UPDATE provider_settings SET credential_ref=NULL,status='unconfigured',updated_at=? WHERE provider=?",(db.now(),provider))
    return dict(ok=True)

def parse_link(text):
    match=re.search(r'https?://[^\s<>]+',text)
    if not match: raise ValueError('请输入作品链接')
    url=match.group().rstrip('，。)）'); parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.username or parsed.password: raise ValueError('链接无效')
    host=parsed.hostname
    if host in ['xiaohongshu.com','www.xiaohongshu.com']:
        m=re.fullmatch(r'/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?',parsed.path)
        if m: return dict(platform='xiaohongshu',post_id=m.group(1),url='https://www.xiaohongshu.com/explore/'+m.group(1))
    if host in ['www.douyin.com','douyin.com']:
        m=re.fullmatch(r'/video/(\d+)/?',parsed.path)
        if m: return dict(platform='douyin',post_id=m.group(1),url='https://www.douyin.com/video/'+m.group(1))
    raise ValueError('请使用作品的完整详情链接（小红书 explore 或抖音 video），当前未解析短链接')

def ocr(evidence_id,method='GeneralBasicOCR'):
    if method not in ['GeneralBasicOCR','GeneralAccurateOCR']:raise ValueError('OCR方法无效')
    with db.connect() as c: item=db.one(c,'SELECT * FROM evidence WHERE id=?',(evidence_id,))
    if not item: raise ValueError('截图不存在')
    with db.connect() as c: cached=db.one(c,'SELECT parser_json FROM ocr_observations WHERE evidence_id=? AND method=?',(evidence_id,method))
    if cached:return db.load(cached['parser_json'])
    if item['parser_json'] and method=='GeneralBasicOCR': return db.load(item['parser_json'])
    with db.connect() as c: configured=db.one(c,"SELECT credential_ref FROM provider_settings WHERE provider='tencent-ocr'")
    ref=configured['credential_ref'] if configured else None
    try:response=tencent_request(get_secret(ref),db.confined(item['relative_path']).read_bytes(),method)
    except ProviderError as exc:
        if 'Auth' in str(exc):
            with db.connect() as c:c.execute("UPDATE provider_settings SET status='blocked_auth',updated_at=? WHERE provider='tencent-ocr' AND credential_ref=?",(db.now(),ref))
        raise
    blocks=[dict(text=x['DetectedText'],confidence=x.get('Confidence'),polygon=x.get('Polygon')) for x in response.get('TextDetections',[])]
    parsed=parse_ocr(blocks)
    with db.connect() as c:
        raw=db.dump(dict(provider='tencent',method=method,request_id=response.get('RequestId'),blocks=blocks))
        c.execute('INSERT INTO ocr_observations VALUES(?,?,?,?,?)',(evidence_id,method,raw,db.dump(parsed),db.now()))
        c.execute('UPDATE evidence SET ocr_json=?,parser_json=? WHERE id=?',(raw,db.dump(parsed),evidence_id))
        c.execute("UPDATE provider_settings SET status='verified',updated_at=? WHERE provider='tencent-ocr' AND credential_ref=?",(db.now(),ref))
    return parsed

def eligible(fans,likes): return fans is not None and likes is not None and fans<5000 and likes>500

def matches(title,rules):
    text=title.casefold()
    return not any(x.casefold() in text for x in rules['exclusions']) and (not rules['keywords'] or any(x.casefold() in text for x in rules['keywords']))

def fetch_references(payload):
    rank=date.fromisoformat(payload['date']).isoformat()
    key=provider_secret('redfox')
    reserve_call('redfox','low_fan_references')
    try:
        response=httpx.get('https://redfox.hk/story/api/cozeSkill/getXhsCozeSkillDataLowFans',headers={'X-API-KEY':key},params={'rankDate':rank,'source':'小红书冷门账号爆款文章','category':payload.get('category','数码科技')},timeout=30,follow_redirects=False)
        if response.status_code!=200: raise ProviderError(f'RedFox 请求失败（HTTP {response.status_code}）；未自动重复付费请求')
        data=response.json()
        if data.get('code')!=2000 or not isinstance(data.get('data'),list): raise ProviderError('RedFox 返回未成功或字段不符，请检查权限及额度')
    except httpx.HTTPError: raise ProviderError('RedFox 网络连接失败，是否计费未知，请手动检查后重试') from None
    total=0
    with db.connect() as c:
        for item in data['data']:
            fans=number(item.get('fans'));likes=number(item.get('useLikeCount'))
            # Approximate counts do not establish strict low-fan boundaries.
            if not str(item.get('fans','')).isdigit() or not str(item.get('useLikeCount','')).isdigit():
                c.execute('INSERT OR IGNORE INTO reference_pending VALUES(?,?,?,?,?)',(sync.digest(item),item.get('title',''),'粉丝或点赞不是可验证的精确计数',db.dump(item),db.now()))
                continue
            if not eligible(fans,likes): continue
            try: link=parse_link(item.get('photoJumpUrl',''))
            except ValueError: continue
            id=link['post_id']
            c.execute('INSERT INTO reference_posts(id,title,url,author,fans,likes,saves,comments,observed_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,fans=excluded.fans,likes=excluded.likes,saves=excluded.saves,comments=excluded.comments,observed_at=excluded.observed_at,payload=excluded.payload',(id,item.get('title',''),link['url'],item.get('userName'),fans,likes,number(item.get('collectedCount')),number(item.get('useCommentCount')),db.now(),db.dump(item)))
            total+=1
            c.execute('INSERT INTO reference_observations VALUES(?,?,?,?)',(db.uid(),id,db.dump(item),db.now()))
        c.execute("UPDATE provider_settings SET status='verified',updated_at=? WHERE provider='redfox'",(db.now(),))
    return dict(count=total)

@router.get('/references')
def references():
    with db.connect() as c:
        rules=db.snapshot(c)['rules']
        items=db.rows(c,'SELECT * FROM reference_posts ORDER BY likes DESC')
        return [dict(**x,payload_data=db.load(x['payload']),preference=db.one(c,'SELECT saved,notes FROM reference_preferences WHERE reference_id=?',(x['id'],)) or dict(saved=0,notes='')) for x in items if matches(x['title']+' '+str(db.load(x['payload']).get('desc','')),rules)]

@router.get('/references/pending')
def pending_references():
    with db.connect() as c:return db.rows(c,'SELECT id,title,reason,observed_at FROM reference_pending ORDER BY observed_at DESC')

@router.patch('/references/{id}')
def save_reference(id:str,value:dict):
    if type(value.get('saved')) is not bool or len(str(value.get('notes','')))>5000: raise ValueError('收藏或笔记格式无效')
    with db.connect() as c:
        if not db.one(c,'SELECT id FROM reference_posts WHERE id=?',(id,)): raise ValueError('案例不存在')
        c.execute('INSERT INTO reference_preferences VALUES(?,?,?) ON CONFLICT(reference_id) DO UPDATE SET saved=excluded.saved,notes=excluded.notes',(id,int(value['saved']),value.get('notes','')))
    return dict(ok=True)

@router.post('/references/search')
def search_references(value:dict):
    date.fromisoformat(value.get('date',''))
    from .worker import enqueue
    return enqueue('references',value,'references:'+sync.digest(value))

@router.get('/trends')
def trends(days:int=7,scope:str='semantic'):
    from datetime import datetime,timezone,timedelta
    if scope not in ['all','related','semantic']:raise ValueError('热榜筛选无效')
    if days not in [7,30]: raise ValueError('请选择7或30天窗口')
    cutoff=datetime.now(timezone.utc);start=cutoff-timedelta(days=days);prior_start=start-timedelta(days=days)
    with db.connect() as c:
        result=[]
        tikhub_active=bool(db.one(c,"SELECT 1 FROM provider_settings WHERE provider='tikhub' AND credential_ref IS NOT NULL"))
        for platform in ['douyin','xiaohongshu','zhihu','bilibili']:
            all_samples=db.rows(c,"SELECT * FROM trend_samples WHERE platform=? AND state='completed' AND collected_at>=? AND collected_at<=? ORDER BY collected_at DESC",(platform,prior_start.isoformat(),cutoff.isoformat()))
            if tikhub_active:all_samples=[sample for sample in all_samples if (db.load(sample['payload']) or [{}])[0].get('source','').startswith('TikHub ')]
            samples=[x for x in all_samples if x['collected_at']>=start.isoformat()]
            prior=[x for x in all_samples if x['collected_at']<start.isoformat()]
            rules=db.snapshot(c)['rules']
            keywords=rules['keywords']
            counts={word:sum(any(word.casefold() in str(item.get('title','')).casefold() for item in db.load(sample['payload'])) for sample in samples) for word in keywords}
            previous={word:sum(any(word.casefold() in str(item.get('title','')).casefold() for item in db.load(sample['payload'])) for sample in prior) for word in keywords}
            displayed=next((sample for sample in samples if db.load(sample['payload'])),None)
            raw=db.load(displayed['payload']) if displayed else []
            related=[dict(**item,source_index=index) for index,item in enumerate(raw) if matches(item.get('title',''),rules)]
            latest=related if scope=='related' else [dict(**item,source_index=index) for index,item in enumerate(raw)]
            from .trend_relevance import context
            _,context_hash=context(c)
            analysis=db.one(c,'SELECT * FROM trend_relevance WHERE snapshot_id=? AND context_hash=?',(displayed['id'],context_hash)) if displayed else None
            analysis_job=None
            if displayed and not analysis:
                candidates=db.rows(c,"SELECT state,error,payload FROM jobs WHERE type='trend_relevance' AND json_extract(payload,'$.context_hash')=? ORDER BY created_at DESC LIMIT 20",(context_hash,))
                analysis_job=next((job for job in candidates if any(batch['id']==displayed['id'] for batch in db.load(job['payload'])['batches'])),None)
            if scope=='semantic':
                latest=[dict(**raw[row['index']],source_index=row['index'],relevance_reason=row['reason'],relevance_score=row['score']) for row in db.load(analysis['payload'])] if analysis else []
                latest.sort(key=lambda item:-item['relevance_score'])
                seen_words=set();unique=[]
                for item in latest:
                    word=str(item.get('title','')).casefold().lstrip('#').strip()
                    if not word or word in seen_words:continue
                    seen_words.add(word);unique.append(item)
                latest=unique
            history={}
            for sample in reversed(samples):
                seen=set()
                for item in db.load(sample['payload']):
                    title=str(item.get('title','')).strip().casefold()
                    if not title or title in seen: continue
                    seen.add(title)
                    record=history.setdefault(title,[])
                    heat=numeric_heat(item.get('heat'))
                    record.append(dict(at=sample['collected_at'],heat=heat))
            for item in latest:
                points=history.get(str(item.get('title','')).strip().casefold(),[])
                item['sample_hits']=len(points)
                numeric=[point for point in points if point['heat'] is not None]
                item['heat_history']=numeric
                item['heat_change_percent']=round((numeric[-1]['heat']/numeric[-2]['heat']-1)*100,1) if len(numeric)>1 and numeric[-2]['heat']>0 else None
            enabled=platform in rules.get('platforms',['douyin','xiaohongshu','zhihu','bilibili'])
            keyword_stats=[]
            for k,v in counts.items():
                current=v/len(samples) if samples else None;base=previous[k]/len(prior) if prior else None
                keyword_stats.append(dict(keyword=k,count=v,coverage=len(samples),ratio=current,baseline_ratio=base,baseline_samples=len(prior),change_percent=(current/base-1)*100 if current is not None and base is not None and base>0 else None))
            source=raw[0].get('source') if raw else None
            fallback='TikHub 暂无可用小红书热搜词' if platform=='xiaohongshu' and tikhub_active else 'TikHub 热门搜索' if tikhub_active else 'RedFox 各平台热点榜' if platform=='xiaohongshu' else 'RedFox 全平台热点事件'
            result.append(dict(platform=platform,state=('ready' if samples else ('not_connected' if platform=='xiaohongshu' else 'not_collected')) if enabled else 'disabled',samples=len(samples),items=latest if enabled else [],keywords=keyword_stats if enabled else [],observed_at=displayed['collected_at'] if displayed else None,latest_attempt_at=samples[0]['collected_at'] if samples else None,using_previous=bool(displayed and samples[0]['id']!=displayed['id']),total_items=len(raw),related_items=len(related),scope=scope,analysis_state='completed' if analysis else analysis_job['state'] if analysis_job else 'pending' if displayed else 'no_data',analysis_error=analysis_job['error'] if analysis_job else None,analysis_at=analysis['checked_at'] if analysis else None,days=days,source=source or fallback,snapshot_id=displayed['id'] if displayed else None))
        return result

@router.post('/trends/analyze')
def analyze_trends():
    from .trend_relevance import enqueue_analysis
    return enqueue_analysis()

@router.post('/trends/refresh')
def refresh_trends(value:dict):
    from datetime import datetime,timedelta
    end=datetime.now().replace(minute=0,second=0,microsecond=0);start=end-timedelta(days=7)
    payload=dict(start=start.strftime('%Y-%m-%d %H:%M:%S'),end=end.strftime('%Y-%m-%d %H:%M:%S'),mode='focused')
    from .worker import enqueue
    return enqueue('trends',payload,'trends:tikhub:'+payload['end'])


@router.get('/hot-topics')
def hot_topics():
    from .hot_topics import latest
    return latest()


@router.post('/hot-topics/refresh')
def refresh_hot_topics():
    from fastapi import HTTPException
    from .hot_topics import enqueue_refresh
    result=enqueue_refresh('manual')
    if result.get('busy'):
        raise HTTPException(409,dict(code='hot_topics_busy',message='热点选题正在更新，请等待本次完成后再试。',retryable=True))
    return result


def store_trend_sample(platform,raw,source):
    """Persist only genuine source hot-word rows; duplicate responses remain one sample."""
    normalized=[]
    for item in raw:
        if not isinstance(item,dict):continue
        title=str(item.get('title') or '').strip()
        if not title:continue
        normalized.append(dict(title=title,url=item.get('url'),rank=item.get('index'),heat=item.get('hotCount'),unit='来源原始热度',upstream_at=item.get('gmtCreate'),source=source))
    if not normalized:return 0
    upstream=max((str(x['upstream_at']) for x in normalized if x['upstream_at']),default=None)
    batch=sync.digest(normalized)
    with db.connect() as c:
        c.execute('INSERT OR IGNORE INTO trend_samples VALUES(?,?,?,?,?,?,?)',(db.uid(),platform,batch,upstream,db.now(),'completed',db.dump(normalized)))
        # A fresh source fetch keeps an unchanged real batch visible without
        # inventing an extra observation or increasing its sample hit count.
        c.execute('UPDATE trend_samples SET collected_at=? WHERE platform=? AND upstream_key=?',(db.now(),platform,batch))
    return len(normalized)


def fetch_focused_trends(payload):
    key=provider_secret('tikhub')
    from .tikhub_trends import request_hot_words
    counts={};errors={}
    for platform in ['douyin','bilibili','zhihu']:
        try:
            items=request_hot_words(key,platform)
            counts[platform]=store_trend_sample(platform,items,'TikHub '+{'douyin':'抖音搜索热榜','bilibili':'B站热门搜索','zhihu':'知乎热门搜索'}[platform])
        except ProviderError as exc:errors[platform]=str(exc)
    if not counts:raise ProviderError('TikHub 热搜词采集均失败：'+'；'.join(errors.values()))
    with db.connect() as c:c.execute("UPDATE provider_settings SET status='verified',updated_at=? WHERE provider='tikhub'",(db.now(),))
    from .trend_relevance import enqueue_analysis
    enqueue_analysis()
    return dict(counts=counts,errors=errors,xiaohongshu='TikHub 现行 API 未提供可用的真实热搜词接口')

def fetch_trends(payload):
    if payload.get('mode')=='focused':return fetch_focused_trends(payload)
    key=provider_secret('redfox')
    reserve_call('redfox','trends')
    try:
        response=httpx.post('https://redfox.hk/story/api/hotSpot/getListByPlatformWithKeyword',headers={'X-API-KEY':key},json=dict(source='全平台热点事件',platforms=[2,8,9],keywords=[],startDate=payload['start'],endDate=payload['end']),timeout=30,follow_redirects=False)
        if response.status_code!=200: raise ProviderError(f'RedFox 热榜请求失败（HTTP {response.status_code}）')
        data=response.json()
        if data.get('code')!=2000 or not isinstance(data.get('data'),dict): raise ProviderError('RedFox 热榜返回未成功或结构不符')
    except httpx.HTTPError: raise ProviderError('热榜网络请求失败；未自动重复调用') from None
    xhs=[]
    if payload.get('include_xiaohongshu'):
        reserve_call('redfox','xiaohongshu_rank')
        try:
            from datetime import datetime,timedelta
            day=datetime.now().date()
            response=httpx.get('https://redfox.hk/story/api/hotSpot/getListByPlatform',headers={'X-API-KEY':key},params={'platform':6,'startDate':str(day-timedelta(days=1)),'endDate':str(day)},timeout=30,follow_redirects=False)
            if response.status_code!=200: raise ProviderError(f'RedFox 小红书榜单请求失败（HTTP {response.status_code}）')
            body=response.json()
            if body.get('code')!=2000 or not isinstance(body.get('data'),list):
                raise ProviderError('RedFox 小红书榜单未返回有效数据：'+str(body.get('message') or body.get('msg') or body.get('code'))[:120])
            xhs=body['data']
        except httpx.HTTPError: raise ProviderError('小红书热榜网络请求失败；未自动重复调用') from None
    counts={}
    with db.connect() as c:
        for name,platform in [('dyList','douyin'),('bzList','bilibili'),('zhList','zhihu'),('xhsList','xiaohongshu')]:
            raw=xhs if platform=='xiaohongshu' and payload.get('include_xiaohongshu') else data['data'].get(name)
            if not isinstance(raw,list): continue
            normalized=[dict(title=str(x.get('title','')),url=x.get('url'),rank=x.get('index'),heat=x.get('hotCount'),unit='来源原始热度',upstream_at=x.get('gmtCreate')) for x in raw]
            upstream=max((str(x['upstream_at']) for x in normalized if x['upstream_at']),default=None)
            # Identical returned batches never increase hit counts on request retries.
            batch=sync.digest(normalized)
            c.execute('INSERT OR IGNORE INTO trend_samples VALUES(?,?,?,?,?,?,?)',(db.uid(),platform,batch,upstream,db.now(),'completed',db.dump(normalized)))
            counts[platform]=len(normalized)
        if not counts: raise ProviderError('RedFox返回缺少已支持平台字段，未记为有效采样')
        c.execute("UPDATE provider_settings SET status='verified',updated_at=? WHERE provider='redfox'",(db.now(),))
    from .trend_relevance import enqueue_analysis
    enqueue_analysis()
    return counts
