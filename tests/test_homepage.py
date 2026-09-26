import time
from unittest.mock import patch, Mock
from backend import db, providers
from backend import homepage_metrics as hm
from test_workspace import client


def save_profile(client, **over):
    p = client.get('/api/profile').json()
    value = {**p['profile'], 'keywords': ['AI'], 'exclusions': [], 'expected_profile_version': p['profile_version'], 'expected_topic_rule_version': p['topic_version'], **over}
    return client.put('/api/profile', json=value)


XHS_URL = 'https://www.xiaohongshu.com/user/profile/61b46d790000000010008153'
DY_URL = 'https://www.douyin.com/user/MS4wLjABAAAANXSltcLCzDGmdNFI2Q_QixVTr67NiYzjKOIP5s03CAE'
SEC_UID = 'MS4wLjABAAAAexample-sec-uid'


def configure_tikhub():
    with db.connect() as c:
        c.execute("INSERT INTO credentials VALUES('cred-homepage','tikhub',x'00','••••test',?)", (db.now(),))
        c.execute("INSERT INTO provider_settings VALUES('tikhub','cred-homepage','saved_unverified',?)", (db.now(),))


def login_douyin(sec_uid=SEC_UID):
    """Simulate a completed QR login: sec_uid lands on the account row."""
    with db.connect() as c:
        c.execute("UPDATE accounts SET sec_uid=?, platform_account_id=? WHERE platform='douyin'", (sec_uid, sec_uid))


def test_profile_saves_validates_and_normalizes_xiaohongshu_homepage(client):
    r = save_profile(client, xiaohongshu_homepage=XHS_URL + '?from=test')
    assert r.status_code == 422, '带查询串的链接应被拒绝'
    r = save_profile(client, xiaohongshu_homepage='https://example.com/user/profile/61b46d790000000010008153')
    assert r.status_code == 422
    r = save_profile(client, xiaohongshu_homepage=DY_URL)
    assert r.status_code == 422, '平台与链接不符应被拒绝'
    r = save_profile(client, xiaohongshu_homepage=XHS_URL)
    assert r.status_code == 200, r.text
    homepages = client.get('/api/profile').json()['homepages']
    assert homepages == {'xiaohongshu': XHS_URL, 'douyin': None}
    r = save_profile(client, xiaohongshu_homepage='')
    assert r.status_code == 200
    assert client.get('/api/profile').json()['homepages'] == {'xiaohongshu': None, 'douyin': None}


def test_profile_saves_validates_and_normalizes_douyin_homepage(client):
    r = save_profile(client, douyin_homepage=XHS_URL)
    assert r.status_code == 422, '小红书链接填入抖音字段应被拒绝'
    r = save_profile(client, douyin_homepage='https://v.douyin.com/3eRRxNgvHY8/')
    assert r.status_code == 200, r.text
    homepages = client.get('/api/profile').json()['homepages']
    assert homepages['douyin'] == 'https://v.douyin.com/3eRRxNgvHY8/'
    r = save_profile(client, douyin_homepage=DY_URL + '?x=1')
    assert r.status_code == 422, '带查询串的抖音链接应被拒绝'
    r = save_profile(client, douyin_homepage='')
    assert r.status_code == 200
    assert client.get('/api/profile').json()['homepages']['douyin'] is None


def test_fetch_douyin_from_homepage_link_without_login(client):
    """No QR login anywhere: the douyin homepage link alone drives sec_uid resolution.
    A numeric douyin-ID link resolves via profile_v2; a sec_uid link needs no call at all."""
    assert save_profile(client, douyin_homepage='https://www.douyin.com/user/42168387231').status_code == 200
    result = run_fetch('job-dy-id', {}, None)
    assert not result['errors'] and len(result['items']) == 1
    assert result['items'][0]['note_id'] == 'douyin'
    accounts = {a['platform']: a for a in client.get('/api/dashboard').json()['accounts']}
    assert accounts['douyin']['metrics']['values'] == {'fans': 1234, 'published_count': 56}
    with db.connect() as c:
        assert c.execute("SELECT sec_uid FROM accounts WHERE platform='douyin'").fetchone()[0] == SEC_UID, '解析出的 sec_uid 应落库复用'
    # A sec_uid-shaped link resolves locally without any TikHub call.
    assert save_profile(client, douyin_homepage='https://www.douyin.com/user/' + SEC_UID).status_code == 200
    with db.connect() as c:
        c.execute("UPDATE accounts SET sec_uid=NULL WHERE platform='douyin'")
    result = run_fetch('job-dy-sec', {}, None)
    assert not result['errors']
    with db.connect() as c:
        assert c.execute("SELECT sec_uid FROM accounts WHERE platform='douyin'").fetchone()[0] == SEC_UID


def test_refresh_requires_tikhub_and_either_source(client):
    assert client.post('/api/accounts/homepage/refresh').status_code == 400
    configure_tikhub()
    assert client.post('/api/accounts/homepage/refresh').status_code == 400, '无小红书链接且未登录抖音时应拒绝'
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    r = client.post('/api/accounts/homepage/refresh')
    assert r.status_code == 200 and r.json()['state'] == 'queued', r.text
    again = client.post('/api/accounts/homepage/refresh')
    assert again.status_code == 200 and (again.json().get('busy') or again.json()['id'] == r.json()['id'])


def fake_request(secret, path, params):
    if path == '/douyin/web/handler_user_profile_v2': return {'sec_uid': SEC_UID}
    if path == '/douyin/app/v3/handler_user_profile': return {'user': {'follower_count': 1234, 'aweme_count': 56}}
    if path == '/xiaohongshu/app_v2/get_user_info':
        # Real app_v2 shape: the user object sits under data.data.
        return {'success': True, 'data': {'fans': 888, 'ndiscovery': 42, 'note_num_stat': {'collected': 5, 'liked': 6, 'posted': 42}}, 'code': 0, 'msg': '成功'}
    raise AssertionError('unexpected ' + path)


def run_fetch(job_id, homepages=None, sec_uid=None):
    with patch.object(providers, 'provider_secret', return_value='test-key'), patch.object(hm, '_request', side_effect=fake_request):
        return hm.run_homepage_fetch(job_id, dict(homepages=homepages or {}, sec_uid=sec_uid))


def test_fetch_writes_snapshots_shows_on_dashboard_and_is_idempotent(client):
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    login_douyin()
    result = run_fetch('job-1', {'xiaohongshu': XHS_URL}, SEC_UID)
    assert not result['errors'] and len(result['items']) == 2
    accounts = {a['platform']: a for a in client.get('/api/dashboard').json()['accounts']}
    assert accounts['xiaohongshu']['metrics']['values'] == {'fans': 888, 'published_count': 42}
    assert accounts['douyin']['metrics']['values'] == {'fans': 1234, 'published_count': 56}
    assert accounts['douyin']['metrics']['observed_at']
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM metric_snapshots WHERE source='homepage_api'").fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM provider_calls WHERE provider=\'tikhub\'').fetchone()[0] == 2
    run_fetch('job-1', {'xiaohongshu': XHS_URL}, SEC_UID)
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM metric_snapshots WHERE source='homepage_api'").fetchone()[0] == 2, '同任务重试不得重复写快照'


def test_dashboard_window_anchors_at_first_observation_day(client):
    """Day 1 = the first day with a fans observation, not a trailing 15-day window:
    on the first tracking day start==end==today, so no empty past days appear."""
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    login_douyin()
    run_fetch('job-d1', {'xiaohongshu': XHS_URL}, SEC_UID)
    payload = client.get('/api/dashboard').json()
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    assert payload['start'] == today == payload['end'], '首日记录时窗口从当天开始，不回看空白的过去15天'
    for a in payload['accounts']:
        assert [p['local_date'] for p in a['points']] == [today], '首日各平台只有一个点'


def test_dashboard_points_one_per_day_latest_wins_and_window_limited(client):
    """Chart series: same-day refreshes collapse to the latest fans value; snapshots
    older than the 15-day window are dropped."""
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    login_douyin()
    run_fetch('job-d1', {'xiaohongshu': XHS_URL}, SEC_UID)

    def bumped(secret, path, params):
        if path == '/xiaohongshu/app_v2/get_user_info':
            return {'success': True, 'data': {'fans': 900, 'ndiscovery': 42, 'note_num_stat': {'posted': 42}}, 'code': 0, 'msg': '成功'}
        return fake_request(secret, path, params)

    with patch.object(providers, 'provider_secret', return_value='test-key'), patch.object(hm, '_request', side_effect=bumped):
        hm.run_homepage_fetch('job-d2', dict(homepages={'xiaohongshu': XHS_URL}, sec_uid=SEC_UID))
    from datetime import datetime, timezone, timedelta
    with db.connect() as c:
        oldest = c.execute("SELECT id FROM metric_snapshots WHERE account_id=(SELECT id FROM accounts WHERE platform='xiaohongshu') ORDER BY created_at LIMIT 1").fetchone()[0]
        c.execute("UPDATE metric_snapshots SET observed_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(days=20)).isoformat(), oldest))
    payload = client.get('/api/dashboard').json()
    xhs = next(a for a in payload['accounts'] if a['platform'] == 'xiaohongshu')
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    assert [p['local_date'] for p in xhs['points']] == [today], '同一天多次更新只保留一个点，窗口外的旧观测被剔除'
    assert xhs['points'][0]['value'] == 900, '当天取最新一次观测'
    assert payload['end'] == today
    assert payload['start'] == (datetime.now(timezone(timedelta(hours=8))).date() - timedelta(days=14)).isoformat()


def test_fetch_partial_failure_keeps_success_and_reports_error(client):
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    login_douyin()

    def failing(secret, path, params):
        if path.startswith('/douyin'): raise providers.ProviderError('TikHub 请求失败（HTTP 500）；未自动重复付费调用')
        return fake_request(secret, path, params)

    with patch.object(providers, 'provider_secret', return_value='test-key'), patch.object(hm, '_request', side_effect=failing):
        result = hm.run_homepage_fetch('job-2', dict(homepages={'xiaohongshu': XHS_URL}, sec_uid=SEC_UID))
    assert len(result['errors']) == 1 and 'HTTP 500' in result['errors'][0]['message']
    accounts = {a['platform']: a for a in client.get('/api/dashboard').json()['accounts']}
    assert accounts['xiaohongshu']['metrics']['values']['fans'] == 888
    assert accounts['douyin']['metrics']['values'] == {}


def test_fetch_rejects_unusable_payload_without_writes(client):
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    for body, message in [(None, '字段不符'), ({'user': {}}, '未包含可用'), ({'unrelated': 1}, '未包含可用')]:
        with patch.object(providers, 'provider_secret', return_value='test-key'), patch.object(hm, '_request', return_value=body):
            try:
                hm.run_homepage_fetch('job-3', dict(homepages={'xiaohongshu': XHS_URL}))
                raise AssertionError('应拒绝不可用响应: ' + repr(body))
            except providers.ProviderError as exc:
                assert message in str(exc)
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM metric_snapshots').fetchone()[0] == 0


def test_worker_completes_enqueued_homepage_job(client):
    configure_tikhub()
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    login_douyin()
    with patch.object(providers, 'provider_secret', return_value='test-key'), patch.object(hm, '_request', side_effect=fake_request):
        job = client.post('/api/accounts/homepage/refresh').json()
        row = None
        for _ in range(60):
            jobs = client.get('/api/jobs').json()
            row = next((x for x in jobs if x['id'] == job['id']), None)
            if row and row['state'] not in ('queued', 'running'): break
            time.sleep(0.1)
    assert row['state'] == 'completed', row
    assert len(row['result']['items']) == 2 and not row['result']['errors']


SHORT_URL = 'https://v.douyin.com/iRNBhf5kS/'
SELF_URL = 'https://www.douyin.com/user/self'


def test_douyin_self_view_rejected_with_guidance():
    import pytest
    with pytest.raises(ValueError, match='不含用户ID'):
        hm.parse_homepage(SELF_URL)


def test_douyin_homepage_link_still_resolves_sec_uid_when_present(client):
    """Legacy douyin homepage links stored earlier still work through the resolve branch."""
    assert save_profile(client, xiaohongshu_homepage=XHS_URL).status_code == 200
    with db.connect() as c:
        c.execute("UPDATE accounts SET homepage=? WHERE platform='douyin'", (SHORT_URL,))
    redirect = Mock(url='https://www.douyin.com/user/' + SEC_UID)
    with patch.object(providers, 'provider_secret', return_value='test-key'), patch.object(hm, '_request', side_effect=fake_request), patch.object(hm.httpx, 'get', return_value=redirect):
        result = hm.run_homepage_fetch('job-4', dict(homepages={'xiaohongshu': XHS_URL}))
    assert not result['errors']
    with db.connect() as c:
        assert db.one(c, "SELECT sec_uid FROM accounts WHERE platform='douyin'")['sec_uid'] == SEC_UID
    douyin = next(a for a in client.get('/api/dashboard').json()['accounts'] if a['platform'] == 'douyin')
    assert douyin['metrics']['values'] == {'fans': 1234, 'published_count': 56}


def test_douyin_login_status_roundtrip(client, tmp_path, monkeypatch):
    from backend import douyin_login
    monkeypatch.setattr(providers, 'secret_root', lambda: tmp_path / 'secrets')
    assert client.get('/api/accounts/douyin').json()['logged_in'] is False
    blob = db.dump(dict(web_cookies=[{'name': 'sessionid', 'value': 'x'}], creator_cookies=[], sec_uid=SEC_UID, nickname='测试号', saved_at=db.now()))
    ref = providers.save_secret('douyin_login', blob)
    with db.connect() as c:
        c.execute("INSERT INTO provider_settings VALUES('douyin_login',?, 'verified',?) ON CONFLICT(provider) DO UPDATE SET credential_ref=excluded.credential_ref,status='verified',updated_at=excluded.updated_at", (ref, db.now()))
    login_douyin()
    status = client.get('/api/accounts/douyin').json()
    assert status['logged_in'] and status['nickname'] == '测试号'
    assert client.post('/api/accounts/douyin/logout').status_code == 200
    assert client.get('/api/accounts/douyin').json()['logged_in'] is False
    with db.connect() as c:
        assert db.one(c, "SELECT sec_uid FROM accounts WHERE platform='douyin'")['sec_uid'] is None


def test_xhs_extraction_matches_real_tikhub_payload():
    real = {'success': True, 'data': {'fans': 82, 'follows': 33, 'liked': 389, 'ndiscovery': 22, 'note_num_stat': {'collected': 247, 'liked': 389, 'posted': 22}}, 'code': 0, 'msg': '成功'}
    assert hm.extract_xiaohongshu_user(real) == {'fans': 82.0, 'published_count': 22.0}


def test_douyin_status_freshness_accepts_epoch_ms_ts(tmp_path):
    from backend import douyin_login as dl
    workdir = tmp_path / 'session'
    dl._write_status(workdir, 'waiting_login', 'x')
    assert dl._fresh(dl._read_status(workdir)) is True
    (workdir / 'status.json').write_text(db.dump(dict(phase='waiting_login', message='x', ts=int(time.time() * 1000))), encoding='utf-8')
    assert dl._fresh(dl._read_status(workdir)) is True, 'epoch-ms 时间戳不得被判为会话失效'
    (workdir / 'status.json').write_text(db.dump(dict(phase='waiting_login', message='x', ts=1000)), encoding='utf-8')
    assert dl._fresh(dl._read_status(workdir)) is False


def test_save_secret_allows_large_douyin_cookie_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, 'secret_root', lambda: tmp_path / 'secrets')
    monkeypatch.setattr(db, 'ROOT', tmp_path)
    db.initialize()
    blob = db.dump(dict(web_cookies=[{'name': f'c{i}', 'value': 'v' * 120} for i in range(80)], creator_cookies=[], sec_uid=SEC_UID, nickname='n', saved_at=db.now()))
    assert len(blob) > 10000, '测试前提：抖音 Cookie 组合应可能超过默认上限'
    ref = providers.save_secret('douyin_login', blob, max_length=40000)
    assert db.load(providers.get_secret(ref))['sec_uid'] == SEC_UID


def test_initialize_migrates_legacy_accounts_table(client):
    with db.connect() as c:
        rows = db.rows(c, 'SELECT * FROM accounts')
        c.execute('DROP TABLE accounts')
        c.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY, platform TEXT UNIQUE NOT NULL, nickname TEXT NOT NULL, platform_account_id TEXT)')
        for r in rows:
            c.execute('INSERT INTO accounts VALUES(?,?,?,?)', (r['id'], r['platform'], r['nickname'], r['platform_account_id']))
        assert 'homepage' not in {x['name'] for x in c.execute('PRAGMA table_info(accounts)')}
    db.initialize()
    with db.connect() as c:
        columns = {x['name'] for x in c.execute('PRAGMA table_info(accounts)')}
        assert 'homepage' in columns and 'sec_uid' in columns
    assert client.get('/api/profile').status_code == 200
