"""Account-level homepage metrics via TikHub: one real request per configured platform."""
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter
from . import db, sync

router = APIRouter(prefix='/api')
BASE = 'https://api.tikhub.io/api/v1'
PLATFORM_NAMES = {'xiaohongshu': '小红书', 'douyin': '抖音'}


def parse_homepage(text):
    match = re.search(r'https?://[^\s<>]+', text or '')
    if not match: raise ValueError('请输入个人主页链接')
    url = match.group().rstrip('，。)）'); parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query: raise ValueError('链接无效')
    host = parsed.hostname
    if host in ['xiaohongshu.com', 'www.xiaohongshu.com']:
        m = re.fullmatch(r'/user/profile/([0-9a-fA-F]{16,40})/?', parsed.path)
        if m: return dict(platform='xiaohongshu', url='https://www.xiaohongshu.com/user/profile/' + m.group(1))
        raise ValueError('小红书主页链接应形如 https://www.xiaohongshu.com/user/profile/用户ID')
    if host in ['www.douyin.com', 'douyin.com']:
        if parsed.path.rstrip('/').endswith('/user/self'):
            raise ValueError('这是登录后查看自己主页的地址，不含用户ID。请在抖音搜索自己的抖音号进入公开主页复制地址栏链接，或粘贴手机 App「分享主页」得到的 v.douyin.com 短链')
        m = re.fullmatch(r'/user/([A-Za-z0-9_-]{20,80})/?', parsed.path)
        if m: return dict(platform='douyin', url='https://www.douyin.com/user/' + m.group(1))
        raise ValueError('抖音主页链接应形如 https://www.douyin.com/user/用户ID，或粘贴手机 App「分享主页」得到的 https://v.douyin.com/ 短链')
    if host == 'v.douyin.com':
        m = re.fullmatch(r'/([0-9A-Za-z]{4,24})/?', parsed.path)
        if m: return dict(platform='douyin', url='https://v.douyin.com/' + m.group(1) + '/')
        raise ValueError('抖音分享短链应形如 https://v.douyin.com/xxxx，请复制手机 App「分享主页」得到的链接')
    raise ValueError('仅支持抖音或小红书个人主页链接')


def _request(secret, path, params):
    from .providers import ProviderError
    try:
        response = httpx.get(BASE + path, headers={'Authorization': 'Bearer ' + secret}, params=params, timeout=30, follow_redirects=False)
    except httpx.HTTPError:
        raise ProviderError('TikHub 网络请求失败；未自动重复付费调用') from None
    if response.status_code != 200:
        raise ProviderError(f'TikHub 请求失败（HTTP {response.status_code}）；未自动重复付费调用')
    try:
        body = response.json()
    except ValueError:
        raise ProviderError('TikHub 返回格式异常；未自动重复付费调用') from None
    if not isinstance(body, dict) or body.get('code') != 200:
        detail = str(body.get('message', body))[:200] if isinstance(body, dict) else '响应非对象'
        raise ProviderError('TikHub 返回未成功：' + detail)
    return body.get('data')


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)): return None
    return float(value)


def extract_douyin_user(data):
    from .providers import ProviderError
    user = data.get('user') if isinstance(data, dict) else None
    if not isinstance(user, dict): raise ProviderError('TikHub 抖音用户信息字段不符，未写入任何数据')
    values = {}
    fans = _count(user.get('follower_count'))
    works = _count(user.get('aweme_count'))
    if fans is not None: values['fans'] = fans
    if works is not None: values['published_count'] = works
    if not values: raise ProviderError('TikHub 抖音用户信息未包含可用的粉丝数或作品数')
    return values


def extract_xiaohongshu_user(data):
    from .providers import ProviderError
    user = None
    if isinstance(data, dict):
        # app_v2 wraps the raw payload as {success, data:{...user...}, code, msg};
        # older variants expose user_info / user directly.
        for key in ['user_info', 'user', 'data']:
            if isinstance(data.get(key), dict): user = data[key]; break
        if user is None: user = data
    if not isinstance(user, dict): raise ProviderError('TikHub 小红书用户信息字段不符，未写入任何数据')
    values = {}
    fans = _count(user.get('fans'))
    if fans is not None: values['fans'] = fans
    notes = None
    stat = user.get('note_num_stat')
    if isinstance(stat, dict): notes = _count(stat.get('posted'))
    if notes is None: notes = _count(user.get('ndiscovery'))
    if notes is None: notes = _count(user.get('notes'))
    if notes is not None: values['published_count'] = notes
    if not values: raise ProviderError('TikHub 小红书用户信息未包含可用的粉丝数或笔记数')
    return values


def configured_homepages(c):
    return {a['platform']: a['homepage'] for a in db.rows(c, 'SELECT platform,homepage FROM accounts WHERE homepage IS NOT NULL AND homepage!=\'\'')}


@router.get('/accounts/homepages')
def homepages():
    with db.connect() as c:
        rows = db.rows(c, 'SELECT platform,nickname,homepage FROM accounts')
        return {x['platform']: dict(nickname=x['nickname'], homepage=x['homepage']) for x in rows}


@router.post('/accounts/homepage/refresh')
def refresh_homepage():
    from .providers import ProviderError
    from .worker import enqueue
    with db.connect() as c:
        accounts = {a['platform']: a for a in db.rows(c, 'SELECT * FROM accounts')}
        tikhub = db.one(c, "SELECT credential_ref FROM provider_settings WHERE provider='tikhub'")
    if not tikhub or not tikhub['credential_ref']: raise ValueError('请先在数据源设置中配置 TikHub')
    parsed = {}
    sec_uid = accounts.get('douyin', {}).get('sec_uid')
    if accounts.get('xiaohongshu', {}).get('homepage'):
        link = parse_homepage(accounts['xiaohongshu']['homepage'])
        if link['platform'] != 'xiaohongshu': raise ValueError('小红书账号配置的主页链接不属于该平台，请检查')
        parsed['xiaohongshu'] = link['url']
    elif not sec_uid and not accounts.get('douyin', {}).get('homepage'):
        raise ValueError('请先配置小红书主页链接，或扫码登录抖音后再更新主页数据')
    minute = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M')
    return enqueue('homepage_fetch', dict(homepages=parsed, sec_uid=sec_uid), 'homepage_refresh:' + sync.digest([parsed, sec_uid])[:12] + ':' + minute, exclusive=True)


def run_homepage_fetch(job_id, payload):
    from .providers import provider_secret, ProviderError, reserve_call
    secret = provider_secret('tikhub')
    with db.connect() as c:
        accounts = {a['platform']: a for a in db.rows(c, 'SELECT * FROM accounts')}
    items = []
    for platform in ['xiaohongshu', 'douyin']:
        account = accounts.get(platform)
        if not account: continue
        name = PLATFORM_NAMES[platform]
        try:
            if platform == 'xiaohongshu':
                url = payload['homepages'].get('xiaohongshu')
                if not url: continue
                reserve_call('tikhub', 'homepage_' + platform)
                user_id = url.rsplit('/', 1)[-1]
                data = _request(secret, '/xiaohongshu/app_v2/get_user_info', {'user_id': user_id})
                values = extract_xiaohongshu_user(data)
            else:
                sec_uid = payload.get('sec_uid')
                if not sec_uid and account.get('homepage'):
                    reserve_call('tikhub', 'homepage_douyin_resolve')
                    sec = _request(secret, '/douyin/web/get_sec_user_id', {'url': parse_homepage(account['homepage'])['url']})
                    sec_uid = sec.get('sec_user_id') if isinstance(sec, dict) else None
                    if not isinstance(sec_uid, str) or not sec_uid:
                        raise ProviderError('TikHub 未从主页链接解析出用户ID；推荐在账号画像中扫码登录抖音')
                    with db.connect() as c:
                        c.execute("UPDATE accounts SET sec_uid=?, platform_account_id=? WHERE platform='douyin'", (sec_uid, sec_uid))
                if not sec_uid:
                    raise ProviderError('请先在账号画像与偏好中扫码登录抖音')
                reserve_call('tikhub', 'homepage_douyin_profile')
                data = _request(secret, '/douyin/app/v3/handler_user_profile', {'sec_user_id': sec_uid})
                values = extract_douyin_user(data)
            observed = datetime.now(timezone.utc).isoformat()
            key = sync.digest([job_id, platform])
            with db.connect() as c:
                if not db.one(c, 'SELECT id FROM metric_snapshots WHERE dedupe_key=?', (key,)):
                    snapshot_id = db.uid()
                    c.execute('INSERT INTO metric_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)', (snapshot_id, None, account['id'], None, 'homepage_api', observed, None, None, 'cumulative', key, db.now()))
                    for metric, value in values.items():
                        c.execute('INSERT INTO metric_values VALUES(?,?,?,?,0)', (snapshot_id, metric, value, 'count'))
                    db.audit(c, 'homepage_metrics_saved', snapshot_id)
            summary = '、'.join('粉丝' if k == 'fans' else '作品数' if k == 'published_count' else k for k in values)
            items.append(dict(note_id=platform, state='completed', title=name + '主页数据已更新', message=summary))
        except ProviderError as exc:
            items.append(dict(note_id=platform, state='failed', title=name + '主页数据更新失败', message=str(exc)))
    if not items: raise ProviderError('没有已配置的账号：请配置小红书主页链接，或扫码登录抖音')
    failures = [x for x in items if x['state'] != 'completed']
    if failures and not any(x['state'] == 'completed' for x in items):
        raise ProviderError('；'.join(x['message'] for x in failures))
    return dict(items=items, errors=failures)
