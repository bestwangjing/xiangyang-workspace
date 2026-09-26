"""Pull all published posts from TikHub into the workspace (no local manuscript required).

- Xiaohongshu: app_v2 get_user_posted_notes, cursor pagination from the configured homepage link.
- Douyin: app_v3 fetch_user_post_videos by sec_uid (from QR login, or resolved once from a homepage link),
  plus creator_v2 item overview for play counts when a creator cookie is available.
Metric values land in the same snapshot store as screenshot updates (source='platform_api')."""
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import APIRouter
from . import db, sync
from .homepage_metrics import _request, BASE, PLATFORM_NAMES, parse_homepage, resolve_douyin_sec_uid
from .providers import provider_secret, ProviderError, reserve_call
from .metrics import number as metric_number

router = APIRouter(prefix='/api')
LOCAL_TZ = timezone(timedelta(hours=8))
MAX_PAGES = 15


def _post_request(secret, path, json_body):
    try:
        response = httpx.post(BASE + path, headers={'Authorization': 'Bearer ' + secret}, json=json_body, timeout=30)
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


def _local_published(unix_seconds):
    moment = datetime.fromtimestamp(unix_seconds, LOCAL_TZ)
    return dict(published_local_date=moment.date().isoformat(), published_at=moment.isoformat(), published_precision='second')


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return metric_number(value)


def _douyin_posts(secret, sec_uid):
    posts = []
    cursor = 0
    for _ in range(MAX_PAGES):
        reserve_call('tikhub', 'posts_douyin_page')
        data = _request(secret, '/douyin/app/v3/fetch_user_post_videos', {'sec_user_id': sec_uid, 'max_cursor': cursor, 'count': 20, 'sort_type': 0})
        awemes = (data or {}).get('aweme_list') or []
        for a in awemes:
            if not isinstance(a, dict) or not a.get('aweme_id'):
                continue
            statistics = a.get('statistics') or {}
            values = {}
            for source, target in [('digg_count', 'likes'), ('comment_count', 'comments'), ('collect_count', 'saves'), ('share_count', 'shares'), ('play_count', 'views')]:
                value = _num(statistics.get(source))
                if value:
                    values[target] = value
            cover = None
            video = a.get('video') or {}
            for key in ['cover', 'origin_cover', 'dynamic_cover']:
                urls = (video.get(key) or {}).get('url_list') or []
                if urls:
                    cover = urls[0]
                    break
            if not cover:
                images = a.get('images') or [{}]
                urls = (images[0] or {}).get('url_list') or []
                cover = urls[0] if urls else None
            title = (a.get('desc') or '').splitlines()[0].strip()[:200] or '(未命名作品)'
            posts.append(dict(
                post_id=str(a['aweme_id']),
                title=title,
                kind='image_post' if a.get('images') else 'video',
                cover_url=cover,
                canonical_url='https://www.douyin.com/video/' + str(a['aweme_id']),
                published=_local_published(a['create_time']) if isinstance(a.get('create_time'), (int, float)) and a['create_time'] > 0 else None,
                values=values,
            ))
        if not data.get('has_more') or not awemes:
            break
        cursor = data.get('max_cursor') or 0
    return posts


def _xiaohongshu_posts(secret, user_id):
    posts = []
    cursor = None
    for _ in range(MAX_PAGES):
        params = {'user_id': user_id}
        if cursor:
            params['cursor'] = cursor
        reserve_call('tikhub', 'posts_xiaohongshu_page')
        data = _request(secret, '/xiaohongshu/app_v2/get_user_posted_notes', params)
        holder = data.get('data') if isinstance(data, dict) and isinstance(data.get('data'), dict) else data
        notes = (holder or {}).get('notes') or []
        if not notes:
            break
        for n in notes:
            if not isinstance(n, dict):
                continue
            note_id = n.get('id')
            if not note_id:
                continue
            values = {}
            for source, target in [('likes', 'likes'), ('comments_count', 'comments'), ('collected_count', 'saves'), ('share_count', 'shares')]:
                value = _num(n.get(source))
                if value:
                    values[target] = value
            images = n.get('images_list') or []
            cover_url = (images[0].get('url') or images[0].get('url_size_large')) if images and isinstance(images[0], dict) else None
            created = n.get('create_time')
            published = _local_published(created) if isinstance(created, (int, float)) and not isinstance(created, bool) and created > 1000000000 else None
            posts.append(dict(
                post_id=str(note_id),
                title=(n.get('display_title') or n.get('title') or '').strip()[:200] or '(未命名笔记)',
                kind='video' if n.get('type') == 'video' else 'image_post',
                cover_url=cover_url,
                canonical_url='https://www.xiaohongshu.com/explore/' + str(note_id),
                published=published,
                values=values,
            ))
        last = notes[-1]
        # app_v2 returns no page-level cursor; each note carries the cursor for the page after it.
        cursor = last.get('cursor') or last.get('id')
        if not (holder or {}).get('has_more') or not cursor:
            break
    return posts


def _creator_play_counts(secret, cookie, post_ids):
    result = {}
    for chunk_start in range(0, len(post_ids), 50):
        chunk = post_ids[chunk_start:chunk_start + 50]
        reserve_call('tikhub', 'douyin_creator_overview')
        data = _post_request(secret, '/douyin/creator_v2/fetch_item_overview_data', {'cookie': cookie, 'ids': ','.join(chunk), 'fields': 'metrics'})
        result.update(_extract_plays(data, set(chunk)))
    return result


def _extract_plays(node, wanted):
    """Walk an unknown-shape creator response for id→play count pairs."""
    plays = {}

    def visit(item):
        if isinstance(item, dict):
            identity = item.get('aweme_id') or item.get('item_id') or item.get('id')
            if isinstance(identity, str) and identity in wanted and identity not in plays:
                holder = item.get('metrics') if isinstance(item.get('metrics'), dict) else item
                for key in ['show_cnt', 'play_cnt', 'play_count', 'show_count']:
                    value = _num(holder.get(key))
                    if value:
                        plays[identity] = value
                        break
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(node)
    return plays


def _upsert(c, job_id, account, post, observed):
    existing = db.one(c, 'SELECT id,content_id,published_time_status,cover_url,title_override FROM publications WHERE platform=? AND platform_post_id=?', (account['platform'], post['post_id']))
    if existing:
        publication_id = existing['id']
        sets = []
        args = []
        if post['cover_url'] and not existing['cover_url']:
            sets.append('cover_url=?')
            args.append(post['cover_url'])
        if post['published'] and existing['published_time_status'] != 'confirmed':
            sets += ['published_local_date=?', 'published_at=?', 'published_precision=?', "published_time_status='confirmed'"]
            args += [post['published']['published_local_date'], post['published']['published_at'], post['published']['published_precision']]
            c.execute('INSERT INTO publication_time_history VALUES(?,?,?,?,?,?,?)', (db.uid(), publication_id, None, db.dump(dict(date=existing['published_local_date'], instant=None, precision=existing['published_time_status'])), db.dump(dict(date=post['published']['published_local_date'], instant=post['published']['published_at'], precision='second')), '平台接口自动同步', db.now()))
        if not existing['content_id'] and post['title'] and existing['title_override'] != post['title']:
            sets.append('title_override=?')
            args.append(post['title'])
        if sets:
            sets.append('version=version+1')
            c.execute('UPDATE publications SET ' + ','.join(sets) + ' WHERE id=?', args + [publication_id])
    else:
        publication_id = db.uid()
        published = post['published'] or dict(published_local_date=None, published_at=None, published_precision='unknown')
        c.execute('INSERT INTO publications(id,content_id,account_id,platform,platform_post_id,canonical_url,status,title_override,cover_url,platform_kind,published_local_date,published_at,published_precision,published_time_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (publication_id, None, account['id'], account['platform'], post['post_id'], post['canonical_url'], 'published', post['title'], post['cover_url'], post['kind'], published['published_local_date'], published['published_at'], published['published_precision'], 'confirmed' if post['published'] else 'unknown'))
        if post['published']:
            c.execute('INSERT INTO publication_time_history VALUES(?,?,?,?,?,?,?)', (db.uid(), publication_id, None, db.dump(dict(date=None, instant=None, precision='unknown')), db.dump(dict(date=published['published_local_date'], instant=published['published_at'], precision='second')), '平台接口自动同步', db.now()))
    if post['values']:
        key = sync.digest([job_id, account['platform'], post['post_id']])
        if not db.one(c, 'SELECT id FROM metric_snapshots WHERE dedupe_key=?', (key,)):
            snapshot_id = db.uid()
            c.execute('INSERT INTO metric_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)', (snapshot_id, publication_id, None, None, 'platform_api', observed, None, None, 'cumulative', key, db.now()))
            for metric, value in post['values'].items():
                c.execute('INSERT INTO metric_values VALUES(?,?,?,?,0)', (snapshot_id, metric, value, 'count'))
    return existing is None


def run_posts_fetch(job_id, payload):
    from .douyin_login import douyin_session, cookie_header
    secret = provider_secret('tikhub')
    items = []
    errors = []
    with db.connect() as c:
        accounts = {a['platform']: a for a in db.rows(c, 'SELECT * FROM accounts')}
        dy_homepage = accounts.get('douyin', {}).get('homepage')
        sec_uid = accounts.get('douyin', {}).get('sec_uid') or payload.get('sec_uid')
        xhs_homepage = payload.get('homepages', {}).get('xiaohongshu') or accounts.get('xiaohongshu', {}).get('homepage')
    # Douyin works from either a QR-login sec_uid or the configured homepage link
    # (resolved via TikHub like homepage refresh does) — no login required.
    if not sec_uid and dy_homepage:
        reserve_call('tikhub', 'posts_douyin_resolve')
        sec_uid = resolve_douyin_sec_uid(secret, dy_homepage)
        with db.connect() as c:
            c.execute("UPDATE accounts SET sec_uid=?, platform_account_id=? WHERE platform='douyin'", (sec_uid, sec_uid))
    plans = []
    if xhs_homepage:
        plans.append(('xiaohongshu', xhs_homepage))
    if sec_uid:
        plans.append(('douyin', sec_uid))
    if not plans:
        raise ProviderError('没有可拉取的账号：请先在账号画像中配置小红书或抖音的个人主页链接')
    for platform, identifier in plans:
        name = PLATFORM_NAMES[platform]
        try:
            play_note = ''
            if platform == 'xiaohongshu':
                posts = _xiaohongshu_posts(secret, parse_homepage(identifier)['url'].rsplit('/', 1)[-1])
            else:
                posts = _douyin_posts(secret, identifier)
                blob = douyin_session()
                cookie = cookie_header(blob, 'creator_cookies') if blob else ''
                if cookie and posts:
                    try:
                        plays = _creator_play_counts(secret, cookie, [p['post_id'] for p in posts])
                        for post in posts:
                            if plays.get(post['post_id']):
                                post['values']['views'] = plays[post['post_id']]
                        play_note = f'，播放量 {len(plays)}/{len(posts)} 篇'
                    except ProviderError as exc:
                        play_note = '，播放量获取失败（作品数据已保存）：' + str(exc)[:120]
            observed = datetime.now(timezone.utc).isoformat()
            created = 0
            with db.connect() as c:
                account = accounts[platform]
                for post in posts:
                    created += 1 if _upsert(c, job_id, account, post, observed) else 0
                db.audit(c, 'posts_synced', platform)
            items.append(dict(note_id=platform, state='completed', title=name + '帖子已同步', message=f'共 {len(posts)} 篇（新增 {created}）' + play_note))
        except ProviderError as exc:
            items.append(dict(note_id=platform, state='failed', title=name + '帖子同步失败', message=str(exc)))
            errors.append(dict(platform=platform, message=str(exc)))
    if errors and not any(x['state'] == 'completed' for x in items):
        raise ProviderError('；'.join(x['message'] for x in errors))
    return dict(items=items, errors=errors)


@router.post('/creation/posts/sync')
def sync_posts():
    from .worker import enqueue
    with db.connect() as c:
        tikhub = db.one(c, "SELECT credential_ref FROM provider_settings WHERE provider='tikhub'")
        xhs_homepage = db.one(c, "SELECT homepage FROM accounts WHERE platform='xiaohongshu'")['homepage']
        dy_homepage = db.one(c, "SELECT homepage FROM accounts WHERE platform='douyin'")['homepage']
        sec_uid = db.one(c, "SELECT sec_uid FROM accounts WHERE platform='douyin'")['sec_uid']
    if not tikhub or not tikhub['credential_ref']:
        raise ValueError('请先在数据源设置中配置 TikHub')
    if not xhs_homepage and not sec_uid and not dy_homepage:
        raise ValueError('请先在账号画像与偏好中配置小红书或抖音的个人主页链接')
    minute = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M')
    return enqueue('posts_fetch', dict(homepages={'xiaohongshu': xhs_homepage}, sec_uid=sec_uid), 'posts_sync:' + minute, exclusive=True)
