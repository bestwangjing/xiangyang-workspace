"""Auto post sync: full pull from TikHub into publications without local manuscripts."""
from unittest.mock import patch
from backend import db, providers, worker
from backend import platform_posts as pp
from test_workspace import client, make_publication
from test_homepage import save_profile, configure_tikhub, login_douyin, SEC_UID, XHS_URL, DY_URL

XHS_NOTES = [{'id': 'note-1', 'display_title': '第一篇图文笔记', 'type': 'normal', 'create_time': 1758800000, 'cursor': 'note-1',
              'likes': 123, 'comments_count': 4, 'collected_count': 5, 'share_count': 6, 'view_count': 777,
              'images_list': [{'url': 'https://img.example/xhs-1.jpg'}]},
             {'id': 'note-2', 'display_title': '第二篇视频笔记', 'type': 'video', 'create_time': 1758900000, 'cursor': 'note-2',
              'likes': 10, 'comments_count': 1, 'collected_count': 2, 'share_count': 0,
              'images_list': [{'url': '', 'url_size_large': 'https://img.example/xhs-2.jpg'}]}]
XHS_NOTES_PAGE2 = [{'id': 'note-3', 'display_title': '第三篇图文笔记', 'type': 'normal', 'create_time': 1758700000, 'cursor': 'note-3',
                    'likes': 7, 'comments_count': 0, 'collected_count': 0, 'share_count': 1, 'images_list': []}]
DY_AWEMES = [{'aweme_id': 'aweme-1', 'desc': '第一支视频作品', 'create_time': 1758800100,
              'statistics': {'digg_count': 99, 'comment_count': 3, 'collect_count': 7, 'share_count': 2, 'play_count': 500},
              'video': {'cover': {'url_list': ['https://img.example/dy-1.jpg']}}},
             {'aweme_id': 'aweme-2', 'desc': '第二篇图文作品', 'create_time': 1758800200,
              'statistics': {'digg_count': 26, 'comment_count': 0, 'collect_count': 21, 'share_count': 2, 'play_count': 0},
              'video': {'cover': {'url_list': ['https://img.example/dy-2.heic']}},
              'images': [{'url_list': ['https://img.example/dy-2.heic'], 'download_url_list': ['https://img.example/dy-2.webp']}]}]


def fake_posts_request(secret, path, params):
    if path == '/xiaohongshu/app_v2/get_user_posted_notes':
        if params.get('cursor'):
            assert params['cursor'] == 'note-2', '翻页应使用上一页最后一条笔记的 cursor'
            return {'code': 0, 'success': True, 'msg': '成功', 'data': {'notes': XHS_NOTES_PAGE2, 'has_more': False}}
        return {'code': 0, 'success': True, 'msg': '成功', 'data': {'notes': XHS_NOTES, 'has_more': True}}
    if path == '/douyin/app/v3/fetch_user_post_videos':
        assert params['sec_user_id'] == SEC_UID
        return {'aweme_list': DY_AWEMES, 'has_more': False, 'max_cursor': 99}
    raise AssertionError('unexpected ' + path)


def run_sync(job_id, **payload):
    with patch.object(pp, 'provider_secret', return_value='test-key'), patch.object(pp, '_request', side_effect=fake_posts_request):
        return pp.run_posts_fetch(job_id, dict(homepages=payload.get('homepages', {'xiaohongshu': XHS_URL}), sec_uid=payload.get('sec_uid', SEC_UID)))


def prepare(client):
    worker.worker.stop()
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    login_douyin()


def test_sync_douyin_via_homepage_link_without_login(client):
    """No QR login: a sec_uid homepage link resolves locally and syncs douyin posts."""
    worker.worker.stop()
    assert save_profile(client, douyin_homepage='https://www.douyin.com/user/' + SEC_UID).status_code == 200
    result = run_sync('job-dy-link', homepages={}, sec_uid=None)
    assert not result['errors'] and len(result['items']) == 1
    assert result['items'][0]['note_id'] == 'douyin'
    rows = {r['platform_post_id']: r for r in client.get('/api/publications').json()}
    assert set(rows) == {'aweme-1', 'aweme-2'}
    assert rows['aweme-1']['metrics']['values']['views'] == 500, 'app_v3 自带播放量，无需登录'
    with db.connect() as c:
        assert c.execute("SELECT sec_uid FROM accounts WHERE platform='douyin'").fetchone()[0] == SEC_UID


def test_sync_creates_publications_with_metrics_cover_and_time(client):
    prepare(client)
    result = run_sync('job-p1')
    assert not result['errors'] and len(result['items']) == 2
    rows = {r['platform_post_id']: r for r in client.get('/api/publications').json()}
    assert set(rows) == {'note-1', 'note-2', 'note-3', 'aweme-1', 'aweme-2'}, '两页笔记与抖音作品都应入库'
    assert rows['note-1']['title'] == '第一篇图文笔记'
    assert rows['note-1']['cover_url'] == 'https://img.example/xhs-1.jpg'
    assert rows['note-1']['platform_kind'] == 'image_post'
    assert rows['note-1']['published_local_date'] == '2025-09-25'
    assert rows['note-1']['metrics']['values'] == {'likes': 123, 'comments': 4, 'saves': 5, 'shares': 6, 'views': 777}
    assert rows['note-2']['platform_kind'] == 'video'
    assert rows['note-2']['cover_url'] == 'https://img.example/xhs-2.jpg', '封面为空时应回退 url_size_large'
    assert rows['aweme-1']['platform_kind'] == 'video'
    assert rows['aweme-1']['metrics']['values']['likes'] == 99
    assert rows['aweme-2']['platform_kind'] == 'image_post'
    assert rows['aweme-2']['cover_url'] == 'https://img.example/dy-2.webp', '图文作品封面应取 webp 下载地址（url_list 为浏览器不渲染的 heic）'
    assert rows['aweme-2']['metrics']['values'] == {'likes': 26, 'saves': 21, 'shares': 2}, '为零的指标不写入'
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM metric_snapshots WHERE source='platform_api'").fetchone()[0] == 5
        assert c.execute('SELECT count(*) FROM publications').fetchone()[0] == 5


def test_sync_is_idempotent_per_job_and_incremental_across_jobs(client):
    prepare(client)
    run_sync('job-p1')
    run_sync('job-p1')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publications').fetchone()[0] == 5, '同任务重试不得重复建作品'
        assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0] == 5, '同任务重试不得重复写快照'
    run_sync('job-p2')
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publications').fetchone()[0] == 5, '增量同步复用已有作品行'
        assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0] == 10, '新一次同步记录新的观测'


def test_sync_preserves_confirmed_time_and_manual_title(client):
    prepare(client)
    existing = make_publication('xiaohongshu', title='旧标题', post_id='note-1', published_local_date='2020-01-01')
    with db.connect() as c:
        c.execute("UPDATE publications SET published_time_status='confirmed' WHERE id=?", (existing['id'],))
    assert client.patch('/api/publications/'+existing['id'], json=dict(title='人工确认过的标题', expected_version=1)).status_code == 200
    run_sync('job-p1')
    row = next(r for r in client.get('/api/publications').json() if r['platform_post_id'] == 'note-1')
    assert row['id'] == existing['id']
    assert row['title'] == '人工确认过的标题', '人工编辑标题不被自动同步覆盖'
    assert row['published_local_date'] == '2020-01-01', '已确认的发布时间不被平台数据覆盖'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM publication_time_history WHERE publication_id=?', (existing['id'],)).fetchone()[0] == 0, '已确认时间不得产生变更记录（抖音新帖的时间历史属预期）'


def test_sync_endpoint_validates_configuration(client):
    worker.worker.stop()
    assert client.post('/api/creation/posts/sync').status_code == 400
    configure_tikhub()
    assert client.post('/api/creation/posts/sync').status_code == 400, '无任何账号来源时应拒绝'
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    r = client.post('/api/creation/posts/sync')
    assert r.status_code == 200 and r.json()['state'] == 'queued', r.text


def test_creator_cookie_enriches_douyin_play_counts(client, tmp_path, monkeypatch):
    prepare(client)
    monkeypatch.setattr(providers, 'secret_root', lambda: tmp_path / 'secrets')
    blob = db.dump(dict(web_cookies=[], creator_cookies=[{'name': 'sessionid', 'value': 'creator'}], sec_uid=SEC_UID, nickname='测试号', saved_at=db.now()))
    ref = providers.save_secret('douyin_login', blob)
    with db.connect() as c:
        c.execute("INSERT INTO provider_settings VALUES('douyin_login',?, 'verified',?) ON CONFLICT(provider) DO UPDATE SET credential_ref=excluded.credential_ref,status='verified',updated_at=excluded.updated_at", (ref, db.now()))

    def fake_overview(secret, path, body):
        assert path == '/douyin/creator_v2/fetch_item_overview_data'
        return {'data': {'list': [{'aweme_id': 'aweme-1', 'metrics': {'show_cnt': 800}}]}}

    with patch.object(pp, 'provider_secret', return_value='test-key'), \
         patch.object(pp, '_request', side_effect=fake_posts_request), \
         patch.object(pp, '_post_request', side_effect=fake_overview):
        result = pp.run_posts_fetch('job-p3', dict(homepages={'xiaohongshu': XHS_URL}, sec_uid=SEC_UID))
    assert not result['errors']
    douyin = next(r for r in client.get('/api/publications').json() if r['platform_post_id'] == 'aweme-1')
    assert douyin['metrics']['values']['views'] == 800, '创作者接口播放量应覆盖公开统计'


def test_sync_failure_reports_partial_state(client):
    prepare(client)

    def failing_douyin(secret, path, params):
        if path.startswith('/douyin'): raise providers.ProviderError('TikHub 请求失败（HTTP 500）；未自动重复付费调用')
        return fake_posts_request(secret, path, params)

    with patch.object(pp, 'provider_secret', return_value='test-key'), patch.object(pp, '_request', side_effect=failing_douyin):
        result = pp.run_posts_fetch('job-p4', dict(homepages={'xiaohongshu': XHS_URL}, sec_uid=SEC_UID))
    assert len(result['errors']) == 1 and result['items'][0]['state'] == 'completed'
    assert {r['platform'] for r in client.get('/api/publications').json()} == {'xiaohongshu'}


def test_sync_without_any_source_raises(client):
    worker.worker.stop()
    with patch.object(pp, 'provider_secret', return_value='test-key'):
        try:
            pp.run_posts_fetch('job-p5', dict(homepages={}, sec_uid=None))
            raise AssertionError('无账号来源应报错')
        except providers.ProviderError as exc:
            assert '没有可拉取的账号' in str(exc)
