"""Douyin QR login: spawns a local Playwright browser window for scanning.

The session cookies travel over the child process stdout pipe only and are
immediately encrypted with Windows DPAPI (providers.save_secret); nothing
sensitive is ever written to disk in plaintext. The temporary browser profile
directory is deleted once the watcher thread finishes.
"""
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter
from . import db, providers

router = APIRouter(prefix='/api')
PROVIDER = 'douyin_login'
IN_PROGRESS = {'open', 'waiting_login', 'login_ok', 'creator', 'saving'}
STALE_AFTER_SECONDS = 10 * 60


def _root(): return db.ROOT / 'login'


def _write_status(workdir, phase, message):
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / 'status.json').write_text(db.dump(dict(phase=phase, message=message, ts=datetime.now(timezone.utc).isoformat(), stale_after_seconds=STALE_AFTER_SECONDS)), encoding='utf-8')


def _read_status(workdir):
    try:
        return db.load((workdir / 'status.json').read_text(encoding='utf-8'))
    except Exception:
        return None


def _fresh(status):
    if not status or status.get('phase') not in IN_PROGRESS:
        return False
    ts = status.get('ts')
    try:
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            stamp = datetime.fromtimestamp(ts / 1000.0, timezone.utc)
        else:
            stamp = datetime.fromisoformat(str(ts))
    except Exception:
        return False
    return (datetime.now(timezone.utc) - stamp).total_seconds() < STALE_AFTER_SECONDS


def douyin_session():
    """Decrypted douyin login blob, or None when not logged in."""
    with db.connect() as c:
        row = db.one(c, 'SELECT credential_ref FROM provider_settings WHERE provider=?', (PROVIDER,))
        ref = row['credential_ref'] if row else None
    if not ref:
        return None
    try:
        return db.load(providers.get_secret(ref))
    except providers.ProviderError:
        return None


def cookie_header(blob, key):
    return '; '.join(f"{c['name']}={c['value']}" for c in blob.get(key) or [])


def _save_result(workdir, payload):
    if not payload.get('sec_uid'):
        _write_status(workdir, 'failed', '登录成功但未能读取主页用户ID，请重新发起登录')
        return
    blob = db.dump(dict(
        web_cookies=payload.get('web_cookies') or [],
        creator_cookies=payload.get('creator_cookies') or [],
        sec_uid=payload['sec_uid'],
        nickname=payload.get('nickname'),
        saved_at=db.now(),
    ))
    ref = providers.save_secret(PROVIDER, blob, max_length=40000)
    with db.connect() as c:
        c.execute("INSERT INTO provider_settings VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET credential_ref=excluded.credential_ref,status='verified',updated_at=excluded.updated_at", (PROVIDER, ref, 'verified', db.now()))
        c.execute("UPDATE accounts SET sec_uid=?, platform_account_id=? WHERE platform='douyin'", (payload['sec_uid'], payload['sec_uid']))
        db.audit(c, 'douyin_login_saved', payload.get('nickname') or '')
    _write_status(workdir, 'done', '抖音登录成功' + ('：' + payload['nickname'] if payload.get('nickname') else ''))


def _watch(proc, workdir):
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith('@@RESULT@@'):
                _save_result(workdir, json.loads(line[len('@@RESULT@@'):]))
                return
        status = _read_status(workdir)
        if not status or status.get('phase') != 'failed':
            _write_status(workdir, 'failed', '登录窗口已关闭或超时，请重新发起')
    except Exception as exc:
        _write_status(workdir, 'failed', '登录结果处理失败：' + str(exc)[:200])
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(workdir / 'browser-profile', ignore_errors=True)


def _spawn(workdir):
    node = shutil.which('node')
    if not node:
        raise ValueError('未找到 Node.js，无法启动扫码登录；请确认电脑已安装 Node')
    script = Path(__file__).resolve().parent.parent / 'scripts' / 'douyin-login.mjs'
    if not script.is_file():
        raise ValueError('登录脚本缺失，请重新安装工作台')
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    proc = subprocess.Popen(
        [node, str(script), str(workdir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
        cwd=str(script.parent.parent), creationflags=creationflags,
    )
    threading.Thread(target=_watch, args=(proc, workdir), daemon=True, name='douyin-login-watch').start()
    _write_status(workdir, 'open', '正在启动浏览器，请稍候')


@router.get('/accounts/douyin')
def douyin_status():
    blob = douyin_session()
    with db.connect() as c:
        account = db.one(c, "SELECT sec_uid FROM accounts WHERE platform='douyin'")
    logged_in = bool(blob and account and account['sec_uid'] == blob.get('sec_uid'))
    return dict(logged_in=logged_in, nickname=blob.get('nickname') if blob else None, saved_at=blob.get('saved_at') if blob else None)


@router.post('/accounts/douyin/login')
def start_login():
    workdir = _root() / 'session'
    status = _read_status(workdir)
    if _fresh(status):
        return dict(state=status['phase'], message=status.get('message'), busy=True)
    if douyin_session():
        raise ValueError('抖音已登录；如需更换账号请先退出登录')
    _spawn(workdir)
    return dict(state='open', message='正在启动浏览器，请稍候')


@router.get('/accounts/douyin/login/status')
def login_status():
    status = _read_status(_root() / 'session')
    if not status:
        return dict(state='idle', message=None)
    if status.get('phase') in IN_PROGRESS and not _fresh(status):
        return dict(state='failed', message='登录会话已失效（超时或服务重启），请重新发起')
    return dict(state=status.get('phase'), message=status.get('message'), nickname=None)


@router.post('/accounts/douyin/logout')
def logout():
    with db.connect() as c:
        c.execute("UPDATE provider_settings SET credential_ref=NULL,status='unconfigured',updated_at=? WHERE provider=?", (db.now(), PROVIDER))
        c.execute("UPDATE accounts SET sec_uid=NULL, platform_account_id=NULL WHERE platform='douyin'")
        db.audit(c, 'douyin_logout', None)
    shutil.rmtree(_root(), ignore_errors=True)
    return dict(ok=True)
