"""Post detail sync: pull title/body/cover/media from TikHub into post_details."""
from unittest.mock import patch
from backend import db, worker
from backend import post_detail as pd
from test_workspace import client, make_publication  # noqa: F401

XHS_IMAGE_NOTE = dict(
    title='只需半小时｜拥有你的自媒体工作台',
    desc='找选题、看热搜、拆爆款……\n工作流正文第二行',
    type='normal',
    id='note-x1',
    liked_count=10, collected_count=15, comments_count=45, share_count=3,
    images_list=[dict(url='https://img.example/x1.jpg'), dict(url='https://img.example/x2.jpg'), dict(url='https://img.example/x3.jpg')],
)
XHS_VIDEO_NOTE = dict(
    title='一个服务行业的AI智能体',
    desc='真实案例分享',
    type='video',
    liked_count=2, collected_count=1, comments_count=0,
    images_list=[dict(url='https://img.example/cover.jpg')],
    video_info_v2=dict(media=dict(stream=dict(h265=[dict(master_url='http://cdn.example/h265.mp4')], h264=[dict(master_url='http://cdn.example/h264.mp4', backup_urls=['http://cdn.example/b.mp4'])]))),
)
DY_AWEME = dict(
    desc='只需半小时\n\n所以这次我把整条工作流，装进了一个本地工作台',
    images=[dict(url_list=['https://img.example/dy-heic.heic', 'https://img.example/dy1.jpg'], download_url_list=['https://img.example/dy1-dl.jpg'])],
    video=dict(cover=dict(url_list=['https://img.example/dy-cover.jpg']), play_addr=dict(url_list=['https://v.example/play.mp4'])),
    statistics=dict(digg_count=7, comment_count=2, collect_count=5, share_count=1),
)


def fake_detail_request(secret, path, params):
    if path == '/xiaohongshu/app_v2/get_image_note_detail':
        return dict(data=[dict(note_list=[XHS_IMAGE_NOTE])])
    if path == '/xiaohongshu/app_v2/get_video_note_detail':
        return dict(data=[XHS_VIDEO_NOTE])
    if path == '/douyin/app/v3/fetch_one_video_v3':
        return dict(aweme_detail=DY_AWEME)
    raise AssertionError('unexpected ' + path)


def run_detail(client_obj, publication_id):
    with patch.object(pd, 'provider_secret', return_value='test-key'), patch.object(pd, '_request', side_effect=fake_detail_request):
        return client_obj.post('/api/publications/' + publication_id + '/sync-detail')


def test_sync_xhs_image_note_stores_body_images_and_metrics(client):  # noqa: F811
    worker.worker.stop()
    pub = make_publication('xiaohongshu', post_id='note-x1', title='旧列表标题')
    with patch.object(pd, 'reserve_call'):
        response = run_detail(client, pub['id'])
    assert response.status_code == 200, response.text
    detail = response.json()['detail']
    assert detail['title'] == '只需半小时｜拥有你的自媒体工作台'
    assert '工作流正文第二行' in detail['body_text']
    assert [m['url'] for m in detail['media'] if m['type'] == 'image'] == ['https://img.example/x1.jpg', 'https://img.example/x2.jpg', 'https://img.example/x3.jpg']
    with db.connect() as c:
        row = db.one(c, 'SELECT * FROM post_details WHERE publication_id=?', (pub['id'],))
        assert row and row['title'] == detail['title'] and db.load(row['media_json']) == detail['media']
        stored = client.get('/api/publications/' + pub['id'] + '/detail').json()['detail']
        assert stored['body_text'] == detail['body_text']
        assert c.execute("SELECT count(*) FROM metric_snapshots WHERE publication_id=? AND source='platform_api'", (pub['id'],)).fetchone()[0] == 1
        latest = client.get('/api/publications').json()
        row = next(x for x in latest if x['id'] == pub['id'])
        assert row['metrics']['values'] == {'likes': 10, 'saves': 15, 'comments': 45, 'shares': 3}


def test_sync_xhs_video_note_prefers_h264_stream(client):  # noqa: F811
    worker.worker.stop()
    pub = make_publication('xiaohongshu', post_id='note-v1', title='视频笔记')
    with db.connect() as c:
        c.execute("UPDATE publications SET platform_kind='video' WHERE id=?", (pub['id'],))
    with patch.object(pd, 'reserve_call'):
        response = run_detail(client, pub['id'])
    detail = response.json()['detail']
    videos = [m for m in detail['media'] if m['type'] == 'video']
    images = [m for m in detail['media'] if m['type'] == 'image']
    assert [v['url'] for v in videos] == ['http://cdn.example/h264.mp4'], 'h264 应优先于 h265'
    assert [i['url'] for i in images] == ['https://img.example/cover.jpg']


def test_sync_douyin_image_post_uses_renderable_download_url(client):  # noqa: F811
    worker.worker.stop()
    pub = make_publication('douyin', post_id='aweme-1', title='抖音图文')
    with patch.object(pd, 'reserve_call'):
        response = run_detail(client, pub['id'])
    detail = response.json()['detail']
    assert detail['title'] == '只需半小时'
    urls = [m['url'] for m in detail['media']]
    assert urls == ['https://img.example/dy1-dl.jpg'], 'heic 应被跳过，优先 download_url_list'
    latest = client.get('/api/publications').json()
    row = next(x for x in latest if x['id'] == pub['id'])
    assert row['metrics']['values'] == {'likes': 7, 'comments': 2, 'saves': 5, 'shares': 1}


def test_detail_endpoints_before_sync_and_same_day_dedupe(client):  # noqa: F811
    worker.worker.stop()
    pub = make_publication('xiaohongshu', post_id='note-x1', title='未同步')
    assert client.get('/api/publications/' + pub['id'] + '/detail').json()['detail'] is None
    with patch.object(pd, 'reserve_call'):
        run_detail(client, pub['id'])
        run_detail(client, pub['id'])
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM metric_snapshots WHERE publication_id=? AND source='platform_api'", (pub['id'],)).fetchone()[0] == 1, '同日重复同步不得重复写快照'


def test_transient_400_is_retried_once(client):  # noqa: F811
    worker.worker.stop()
    pub = make_publication('xiaohongshu', post_id='note-x1', title='重试')
    calls = {'n': 0}

    def flaky(secret, path, params):
        calls['n'] += 1
        if calls['n'] == 1:
            raise pd.ProviderError('TikHub 请求失败（HTTP 400）；未自动重复付费调用')
        return fake_detail_request(secret, path, params)

    with patch.object(pd, 'provider_secret', return_value='test-key'), patch.object(pd, '_request', side_effect=flaky), patch.object(pd, 'reserve_call'):
        response = client.post('/api/publications/' + pub['id'] + '/sync-detail')
    assert response.status_code == 200 and calls['n'] == 2


def test_topic_detail_first_sync_then_reads_local_cache(client):  # noqa: F811
    worker.worker.stop()
    topic_id = db.uid()
    source = dict(platform='xiaohongshu', post_id='note-x1', title='来源标题', content_type='图文', cover_url='https://img.example/list-cover.jpg')
    with db.connect() as c:
        snapshot = db.snapshot(c)
        c.execute('INSERT INTO topic_candidates VALUES(?,?,?,?,?,?,?)',
                  (topic_id, '我的选题', db.dump(dict(source=source)), 'saved', snapshot['profile_version'], snapshot['topic_version'], db.now()))

    assert client.get('/api/topics/' + topic_id + '/detail').json()['detail'] is None
    with patch.object(pd, 'provider_secret', return_value='test-key'), patch.object(pd, '_request', side_effect=fake_detail_request), patch.object(pd, 'reserve_call') as budget:
        response = client.post('/api/topics/' + topic_id + '/sync-detail')
    assert response.status_code == 200, response.text
    detail = response.json()['detail']
    assert detail['title'] == XHS_IMAGE_NOTE['title']
    assert detail['cover_url'] == 'https://img.example/x1.jpg'
    assert len(detail['media']) == 3
    budget.assert_called_once_with('tikhub', 'post_detail')

    # Reopening the detail page must use SQLite and not call the paid provider again.
    with patch.object(pd, '_request', side_effect=AssertionError('cache read must not request provider')):
        cached = client.get('/api/topics/' + topic_id + '/detail').json()['detail']
    assert cached == detail
    listed = next(item for item in client.get('/api/topics').json() if item['id'] == topic_id)
    assert listed['source_detail']['media'][1]['url'] == 'https://img.example/x2.jpg'
