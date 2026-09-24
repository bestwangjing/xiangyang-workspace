import re
from datetime import date,datetime
from . import db,sync

METRICS={'fans','published_count','views','likes','saves','comments','shares','new_follows','impressions','completion_rate','watch_seconds','click_rate'}

def effective_values(c,snapshot_id):
    values={x['metric_key']:x['value'] for x in db.rows(c,'SELECT * FROM metric_values WHERE snapshot_id=?',(snapshot_id,))}
    for correction in db.rows(c,'SELECT * FROM metric_corrections WHERE snapshot_id=? ORDER BY id',(snapshot_id,)):
        values[correction['metric_key']]=correction['new_value']
    return values

def number(text):
    match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(万|千|w|W|k|K)?\s*',str(text).replace(',',''))
    if not match: return None
    return float(match.group(1))*({'万':10000,'w':10000,'W':10000,'千':1000,'k':1000,'K':1000}.get(match.group(2),1))

def parse_ocr(blocks):
    labels={'粉丝总数':'fans','粉丝':'fans','作品数':'published_count','阅读量':'views','播放量':'views','点赞':'likes','收藏':'saves','评论':'comments','分享':'shares','新增关注':'new_follows','曝光量':'impressions'}
    candidates=[]
    for i,b in enumerate(blocks):
        text=b['text'].strip()
        for label,key in labels.items():
            if label in text:
                tail=text.split(label,1)[1].strip(' ：:')
                val=number(tail)
                related=b
                if val is None and i+1<len(blocks):
                    related=blocks[i+1]; val=number(related['text'])
                if val is not None: candidates.append(dict(key=key,value=val,raw=text,polygon=related.get('polygon'),confidence=min(b.get('confidence',0),related.get('confidence',0)),status='needs_review'))
    times=[]
    for b in blocks:
        if '发布时间' in b['text']:
            m=re.search(r'(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})',b['text'])
            if m:
                try: parsed=date(*map(int,m.groups())).isoformat()
                except ValueError: continue
                times.append(dict(raw=b['text'],date=parsed,precision='day',polygon=b.get('polygon'),status='needs_review'))
    return dict(metrics=candidates,publication_times=times,blocks=blocks,requires_confirmation=True)

def confirm(evidence_id,value):
    publication_id=value.get('publication_id') or None
    account_id=value.get('account_id') or None
    if bool(publication_id)==bool(account_id): raise ValueError('请选择一个作品或一个账号')
    scope=value.get('scope','cumulative')
    if scope not in ['cumulative','period']: raise ValueError('指标口径无效')
    metrics=value.get('metrics',{})
    for key,num in metrics.items():
        if key not in METRICS or (num is not None and (type(num) not in [int,float] or not __import__('math').isfinite(num) or num<0)): raise ValueError('指标必须是非负有限数值；缺测请留空')
        if key in ['completion_rate','click_rate'] and num is not None and num>100: raise ValueError('百分比请按0至100填写')
    observed=value.get('observed_at') or None
    if observed:
        dt=datetime.fromisoformat(observed)
        if dt.tzinfo is None: raise ValueError('观测时间需包含时区')
        observed=dt.isoformat()
    if scope=='period':
        if not value.get('window_start') or not value.get('window_end'): raise ValueError('区间指标必须提供统计起止日期')
        if date.fromisoformat(value['window_start'])>date.fromisoformat(value['window_end']): raise ValueError('统计区间无效')
    publication_date=value.get('published_local_date') or None
    if publication_date: date.fromisoformat(publication_date)
    key=sync.digest([evidence_id,publication_id,account_id,scope,value.get('window_start'),value.get('window_end')])
    payload_hash=sync.digest([metrics,observed,publication_date])
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        evidence=db.one(c,'SELECT * FROM evidence WHERE id=?',(evidence_id,))
        if not evidence: raise ValueError('原始截图不存在')
        if publication_date and evidence['kind']!='screenshot': raise ValueError('发布时间必须由后台截图核对，API数据不可确认')
        existing=db.one(c,'SELECT * FROM review_queue WHERE id=?',(key,))
        if existing:
            if existing['payload']!=payload_hash:
                if not str(value.get('correction_reason','')).strip(): raise ValueError('此截图已确认其他值；请填写校正原因后保存')
                original=db.one(c,'SELECT observed_at FROM metric_snapshots WHERE id=?',(existing['resolution'],))
                if original['observed_at']!=observed: raise ValueError('指标校正不能同时改变观测时间，请保留原观测时间')
                if publication_date:
                    current=db.one(c,'SELECT published_local_date FROM publications WHERE id=?',(publication_id,))
                    if current['published_local_date']!=publication_date: raise ValueError('发布时间请通过时间证据入口单独校正')
                old=effective_values(c,existing['resolution'])
                for metric,num in metrics.items():
                    if old.get(metric)!=num: c.execute('INSERT INTO metric_corrections(snapshot_id,metric_key,old_value,new_value,reason,created_at) VALUES(?,?,?,?,?,?)',(existing['resolution'],metric,old.get(metric),num,value['correction_reason'],db.now()))
                c.execute('UPDATE review_queue SET payload=?,resolved_at=? WHERE id=?',(payload_hash,db.now(),key))
                db.audit(c,'metrics_corrected',existing['resolution'])
                return dict(id=existing['resolution'],corrected=True)
            return dict(id=existing['resolution'],duplicate=True)
        if publication_id:
            p=db.one(c,'SELECT * FROM publications WHERE id=?',(publication_id,))
            if not p: raise ValueError('发布记录不存在')
            if publication_date:
                if value.get('expected_version')!=p['version']: raise ValueError('发布记录版本变化，请重新加载')
                if p['published_local_date'] and p['published_local_date']!=publication_date and not value.get('correction_reason'): raise ValueError('发布时间冲突，请核对原截图并填写校正原因')
                if not (p['published_local_date']==publication_date and p['published_at']):
                    c.execute('INSERT INTO publication_time_history VALUES(?,?,?,?,?,?,?)',(db.uid(),publication_id,evidence_id,p['published_local_date'],publication_date,value.get('correction_reason','截图人工核对'),db.now()))
                    c.execute("UPDATE publications SET published_local_date=?,published_at=NULL,published_precision='day',published_time_status='confirmed',published_evidence_id=?,version=version+1 WHERE id=?",(publication_date,evidence_id,publication_id))
        elif not db.one(c,'SELECT id FROM accounts WHERE id=?',(account_id,)): raise ValueError('账号不存在')
        id=db.uid()
        c.execute('INSERT INTO metric_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)',(id,publication_id,account_id,evidence_id,'screenshot_confirmed' if evidence['kind']=='screenshot' else 'api_manually_confirmed',observed,value.get('window_start'),value.get('window_end'),scope,key,db.now()))
        for metric,num in metrics.items():
            if num is not None: c.execute('INSERT INTO metric_values VALUES(?,?,?,?,0)',(id,metric,num,'percent' if metric in ['completion_rate','click_rate'] else 'seconds' if metric=='watch_seconds' else 'count'))
        c.execute('INSERT INTO review_queue VALUES(?,?,?,?,?,?)',(key,evidence_id,'confirmed',payload_hash,id,db.now()))
        db.audit(c,'evidence_confirmed',evidence_id)
        return dict(id=id,duplicate=False)
