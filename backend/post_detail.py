"""On-demand post detail sync: title, body, cover and media pulled from TikHub.

- Xiaohongshu: app_v2 splits image notes (get_image_note_detail, note wrapped in
  data.data[0].note_list) and video notes (get_video_note_detail, note at data.data[0]);
  video files sit under video_info_v2.media.stream.
- Douyin: app_v3 fetch_one_video_v3 returns aweme_detail with desc, images (图文) or
  video.play_addr; the endpoint intermittently answers HTTP 400 "not charged" and
  succeeds on one retry.
Details land in post_details; engagement metrics reuse the metric_snapshots store."""
from datetime import datetime, timezone
from fastapi import APIRouter
from . import db, sync
from .homepage_metrics import BASE, _request
from .providers import provider_secret, reserve_call, ProviderError
from .platform_posts import _num, _renderable

router = APIRouter(prefix='/api')


def _fetch(path, params):
    secret = provider_secret('tikhub')
    reserve_call('tikhub', 'post_detail')
    try:
        return _request(secret, path, params)
    except ProviderError as exc:
        if 'HTTP 400' not in str(exc):
            raise
        # TikHub answers transient 400s that are explicitly not charged; one retry.
        reserve_call('tikhub', 'post_detail')
        return _request(secret, path, params)


def _xhs_note(payload):
    inner = payload.get('data') if isinstance(payload, dict) else None
    first = inner[0] if isinstance(inner, list) and inner else inner
    if isinstance(first, dict) and isinstance(first.get('note_list'), list) and first['note_list']:
        return first['note_list'][0]
    return first if isinstance(first, dict) else None


def _xhs_media(note):
    media = []
    if note.get('type') == 'video':
        streams = (((note.get('video_info_v2') or {}).get('media') or {}).get('stream') or {})
        for codec in ['h264', 'h265']:
            entries = streams.get(codec) or []
            if entries and entries[0].get('master_url'):
                media.append(dict(type='video', url=entries[0]['master_url']))
                break
    for image in note.get('images_list') or []:
        url = image.get('url') or image.get('url_size_large')
        if isinstance(image, dict) and url:
            media.append(dict(type='image', url=url))
    return media


def _douyin_media(aweme):
    media = []
    images = aweme.get('images') or []
    if images:
        for image in images:
            url = _renderable(image.get('download_url_list')) or _renderable(image.get('url_list'))
            if url:
                media.append(dict(type='image', url=url))
        return media
    play = ((aweme.get('video') or {}).get('play_addr') or {}).get('url_list') or []
    url = _renderable(play)
    if url:
        media.append(dict(type='video', url=url))
    return media


def _metrics_of(note, fields):
    values = {}
    for source, target in fields:
        value = _num(note.get(source))
        if value:
            values[target] = value
    return values


XHS_METRICS = [('liked_count', 'likes'), ('comments_count', 'comments'), ('collected_count', 'saves'), ('share_count', 'shares')]
DY_METRICS = [('digg_count', 'likes'), ('comment_count', 'comments'), ('collect_count', 'saves'), ('share_count', 'shares')]


def _store(publication_id, detail):
    observed = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        p = db.one(c, 'SELECT * FROM publications WHERE id=?', (publication_id,))
        if not p:
            raise ProviderError('发布记录不存在')
        c.execute('INSERT INTO post_details(publication_id,title,body_text,cover_url,media_json,fetched_at) VALUES(?,?,?,?,?,?) ON CONFLICT(publication_id) DO UPDATE SET title=excluded.title,body_text=excluded.body_text,cover_url=excluded.cover_url,media_json=excluded.media_json,fetched_at=excluded.fetched_at',
                  (publication_id, detail['title'], detail['body_text'], detail['cover_url'], db.dump(detail['media']), observed))
        if detail['cover_url'] and (not p['cover_url'] or ('.heic' in p['cover_url'] and '.heic' not in detail['cover_url'])):
            c.execute('UPDATE publications SET cover_url=?,version=version+1 WHERE id=?', (detail['cover_url'], publication_id))
        if detail['values']:
            key = sync.digest(['post_detail', p['platform'], p['platform_post_id'], observed[:10]])
            if not db.one(c, 'SELECT id FROM metric_snapshots WHERE dedupe_key=?', (key,)):
                snapshot_id = db.uid()
                c.execute('INSERT INTO metric_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)', (snapshot_id, publication_id, None, None, 'platform_api', observed, None, None, 'cumulative', key, db.now()))
                for metric, value in detail['values'].items():
                    c.execute('INSERT INTO metric_values VALUES(?,?,?,?,0)', (snapshot_id, metric, value, 'count'))
        db.audit(c, 'post_detail_synced', publication_id)


def _fetch_detail(publication):
    post_id = publication['platform_post_id']
    if publication['platform'] == 'douyin':
        aweme = (_fetch('/douyin/app/v3/fetch_one_video_v3', {'aweme_id': post_id}) or {}).get('aweme_detail') or {}
        desc = aweme.get('desc') or ''
        return dict(title=desc.splitlines()[0].strip()[:200] or '(未命名作品)', body_text=desc,
                    cover_url=_renderable((aweme.get('video') or {}).get('cover', {}).get('url_list')),
                    media=_douyin_media(aweme), values=_metrics_of(aweme.get('statistics') or {}, DY_METRICS))
    if publication['platform_kind'] == 'video':
        note = _xhs_note(_fetch('/xiaohongshu/app_v2/get_video_note_detail', {'note_id': post_id})) or {}
    else:
        note = _xhs_note(_fetch('/xiaohongshu/app_v2/get_image_note_detail', {'note_id': post_id})) or {}
    return dict(title=(note.get('title') or note.get('display_title') or '').strip()[:200] or '(未命名笔记)',
                body_text=note.get('desc') or '', cover_url=note.get('images_list', [{}])[0].get('url') if note.get('images_list') else None,
                media=_xhs_media(note), values=_metrics_of(note, XHS_METRICS))


def _stored(publication_id):
    with db.connect() as c:
        row = db.one(c, 'SELECT * FROM post_details WHERE publication_id=?', (publication_id,))
        if not row:
            return None
        media = db.load(row['media_json']) if row['media_json'] else []
        return dict(title=row['title'], body_text=row['body_text'], cover_url=row['cover_url'], media=media, fetched_at=row['fetched_at'])


@router.get('/publications/{publication_id}/detail')
def get_detail(publication_id: str):
    return dict(detail=_stored(publication_id))


@router.post('/publications/{publication_id}/sync-detail')
def sync_detail(publication_id: str):
    with db.connect() as c:
        publication = db.one(c, 'SELECT * FROM publications WHERE id=?', (publication_id,))
    if not publication:
        from .main import fail
        fail('发布记录不存在', 404)
    detail = _fetch_detail(publication)
    _store(publication_id, detail)
    return dict(detail=_stored(publication_id))
