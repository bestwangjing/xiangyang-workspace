"""Limited upload-only LAN listener, started explicitly by the local UI."""
import os
import hashlib
import io
import secrets
import socket
import threading
from datetime import datetime,timedelta,timezone
import qrcode
from fastapi import FastAPI,UploadFile,File,Header,HTTPException,APIRouter
from fastapi.responses import HTMLResponse,Response
from . import db

router=APIRouter(prefix='/api/upload-sessions')
phone=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
PORT=int(os.environ.get('XIANGYANG_UPLOAD_PORT','8767'))
server=None
lock=threading.Lock()

@phone.middleware('http')
async def bounded_upload(request,call_next):
    if request.method=='POST':
        length=request.headers.get('content-length','')
        if not length.isdigit() or int(length)>21*1024*1024:
            from fastapi.responses import JSONResponse
            return JSONResponse({'message':'上传请求超过上限或长度未知'},413)
    response=await call_next(request)
    response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    return response

def authorize(token):
    if not token: raise HTTPException(401,'上传会话无效')
    with db.connect() as c: session=db.one(c,'SELECT * FROM upload_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    if not session or session['revoked_at'] or session['expires_at']<db.now(): raise HTTPException(401,'上传会话已过期，请在电脑重新创建')
    return session

PHONE_HTML='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>向阳AI工作台 · 截图上传</title><style>body{font:16px/1.8 system-ui;max-width:560px;margin:40px auto;padding:24px;background:#f7f8fa;color:#303944}main{background:white;padding:28px;border-radius:16px}button{padding:13px 25px;border:0;border-radius:8px;background:#d83d4c;color:white;margin-top:20px}input{max-width:100%}p{color:#7b8592;font-size:14px}</style><main><h2>向阳AI工作台</h2><p>上传后台截图后，回到电脑核对数据。</p><input id="files" type="file" accept="image/png,image/jpeg,image/webp" multiple><br><button id="upload">上传截图</button><p id="status">单次会话最多20张，10分钟有效。</p></main><script>const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);document.getElementById('upload').onclick=async()=>{const button=document.getElementById('upload');button.disabled=true;let n=0;try{for(const file of document.getElementById('files').files){const data=new FormData();data.append('file',file);const r=await fetch('/upload',{method:'POST',headers:{'X-Upload-Token':token},body:data});if(!r.ok)throw new Error('上传失败：会话过期、格式或数量受限');n++;document.getElementById('status').textContent='已上传 '+n+' 张，请回电脑核对。'}}catch(e){document.getElementById('status').textContent=e.message}finally{button.disabled=false}};</script></html>'''

@phone.get('/')
def page(): return HTMLResponse(PHONE_HTML,headers={'Referrer-Policy':'no-referrer','Cache-Control':'no-store','X-Frame-Options':'DENY'})

@phone.post('/upload')
async def upload(file:UploadFile=File(...),x_upload_token:str=Header(default='')):
    session=authorize(x_upload_token)
    raw=await file.read(20*1024*1024+1)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        session=authorize(x_upload_token)
        if session['count']>=20: raise HTTPException(429,'上传数量达到上限')
        c.execute('UPDATE upload_sessions SET count=count+1 WHERE id=?',(session['id'],))
    from .main import store_evidence
    result=store_evidence(raw)
    return dict(ok=True,duplicate=result['duplicate'])

@phone.get('/status')
def upload_status(x_upload_token:str=Header(default='')):
    session=authorize(x_upload_token)
    return dict(count=session['count'],expires_at=session['expires_at'])

@router.post('')
def create_session(value:dict=None):
    mode=(value or {}).get('mode','lan')
    if mode not in ['lan','internet']:raise HTTPException(400,'请选择同一Wi-Fi或跨网络上传')
    global server
    import uvicorn
    with lock:
        if server is None:
            config=uvicorn.Config(phone,host='0.0.0.0',port=PORT,access_log=False,log_level='error')
            server=uvicorn.Server(config)
            threading.Thread(target=server.run,daemon=True,name='upload-only').start()
        import time
        for _ in range(40):
            if server.started: break
            time.sleep(.05)
        if not server.started:
            server=None
            raise HTTPException(503,'手机上传服务未启动，请检查8767端口占用')
    token=secrets.token_urlsafe(32);id=db.uid()
    if mode=='internet':
        from .remote_upload import tunnel
        base=tunnel.start(id)
    else:
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try: sock.connect(('8.8.8.8',80));address=sock.getsockname()[0]
        except OSError: raise HTTPException(503,'未找到可用局域网地址，请先连接Wi-Fi') from None
        finally:sock.close()
        base=f'http://{address}:{PORT}'
    expires=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
    try:
        with db.connect() as c: c.execute('INSERT INTO upload_sessions VALUES(?,?,?,NULL,0)',(id,hashlib.sha256(token.encode()).hexdigest(),expires))
    except Exception:
        if mode=='internet':tunnel.revoke(id)
        raise
    url=base+'/#'+token
    image=qrcode.make(url);b=io.BytesIO();image.save(b,format='PNG')
    import base64
    return dict(id=id,url=url,mode=mode,expires_at=expires,qr='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode())

@router.delete('/{id}')
def revoke(id:str):
    with db.connect() as c: c.execute('UPDATE upload_sessions SET revoked_at=? WHERE id=?',(db.now(),id))
    from .remote_upload import tunnel
    tunnel.revoke(id)
    return dict(ok=True)
