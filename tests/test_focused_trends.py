from unittest.mock import Mock,patch
from backend import db,providers,tikhub_trends
from test_workspace import client


def response(data):
    return {'code':200,'data':data,'time':'2026-09-24 10:00:00'}


def test_tikhub_parses_search_hot_words_not_articles():
    douyin=response({'code':0,'data':{'search_list':[{'key_word':'AI办公','search_score':123},{'title':'普通视频标题'}]}})
    bili=response({'code':0,'data':{'trending':{'list':[{'keyword':'智能体','show_name':'智能体'}]}}})
    zhihu=response({'recommend_queries':{'queries':[{'real_query':'AI工具','label':'hot'},{'real_query':'普通推荐文章','label':''}]}})
    assert [x['title'] for x in tikhub_trends.parse_hot_words('douyin',douyin)]==['AI办公']
    assert [x['title'] for x in tikhub_trends.parse_hot_words('bilibili',bili)]==['智能体']
    assert [x['title'] for x in tikhub_trends.parse_hot_words('zhihu',zhihu)]==['AI工具']
    assert 'xiaohongshu' not in tikhub_trends.FEEDS
    try:tikhub_trends.parse_hot_words('zhihu',response({'articles':[{'title':'文章主题'}]}))
    except providers.ProviderError:pass
    else:assert False,'article titles must not become hot search words'


def test_tikhub_request_uses_actual_search_feed_and_budget(client):
    r=Mock(status_code=200);r.json.return_value=response({'code':0,'data':{'search_list':[{'key_word':'AI办公'}]}})
    with patch.object(providers,'reserve_call') as budget,patch.object(tikhub_trends.httpx,'request',return_value=r) as request:
        assert tikhub_trends.request_hot_words('test-secret','douyin')[0]['title']=='AI办公'
    budget.assert_called_once_with('tikhub','douyin_hot_words')
    assert request.call_args.kwargs['json']['keyword']=='AI'
    assert request.call_args.kwargs['headers']=={'Authorization':'Bearer test-secret'}


def test_focused_refresh_uses_tikhub_and_keeps_four_platforms(client):
    with patch.object(providers,'provider_secret',return_value='test'),patch.object(tikhub_trends,'request_hot_words',side_effect=lambda secret,platform:[{'title':platform+' AI热搜','index':1}]),patch('backend.trend_relevance.enqueue_analysis',return_value={'cached':True}):
        result=providers.fetch_trends({'mode':'focused'})
    assert result['counts']=={'douyin':1,'bilibili':1,'zhihu':1}
    assert 'xiaohongshu' not in result['counts']
    boards=client.get('/api/trends?scope=all').json()
    assert len(boards)==4
    assert all(boards[i]['source'].startswith('TikHub ') for i in (0,2,3))


def test_tikhub_failure_does_not_hide_other_real_words(client):
    def fetch(secret,platform):
        if platform=='bilibili':raise providers.ProviderError('B站接口失败')
        return [{'title':platform+'热搜词','index':1}]
    with patch.object(providers,'provider_secret',return_value='test'),patch.object(tikhub_trends,'request_hot_words',side_effect=fetch),patch('backend.trend_relevance.enqueue_analysis',return_value={'cached':True}):
        result=providers.fetch_trends({'mode':'focused'})
    assert result['counts']=={'douyin':1,'zhihu':1}
    assert result['errors']=={'bilibili':'B站接口失败'}


def test_tikhub_key_is_masked_and_save_auto_validates(client):
    with patch.object(providers,'validation_probe') as probe:
        saved=client.put('/api/providers/tikhub/key',json={'key':'test-tikhub-secret'}).json()
    assert saved['status']=='verified'
    probe.assert_called_once_with('tikhub','test-tikhub-secret')
    status=next(x for x in client.get('/api/providers').json() if x['provider']=='tikhub')
    assert status['status']=='verified'
    assert 'test-tikhub-secret' not in str(status)
    assert providers.provider_secret('tikhub')=='test-tikhub-secret'


def test_refresh_queues_tikhub_query(client):
    result=client.post('/api/trends/refresh',json={}).json()
    with db.connect() as c:job=db.one(c,'SELECT payload FROM jobs WHERE id=?',(result['id'],))
    assert db.load(job['payload'])['mode']=='focused'


def test_semantic_hot_words_hide_case_and_hashtag_duplicates(client):
    from backend.trend_relevance import context
    raw=[{'title':'#AI创作','source':'TikHub 抖音搜索热榜'},{'title':'ai创作','source':'TikHub 抖音搜索热榜'},{'title':'豆包AI','source':'TikHub 抖音搜索热榜'}]
    with db.connect() as c:
        c.execute('INSERT INTO trend_samples VALUES(?,?,?,?,?,?,?)',('dedupe','douyin','dedupe',None,db.now(),'completed',db.dump(raw)))
        _,hashed=context(c)
        c.execute('INSERT INTO trend_relevance VALUES(?,?,?,?,?)',('dedupe',hashed,db.dump([{'index':0,'score':90,'reason':'AI创作'},{'index':1,'score':80,'reason':'AI创作'},{'index':2,'score':75,'reason':'AI工具'}]),'test',db.now()))
    board=client.get('/api/trends').json()[0]
    assert [item['title'] for item in board['items']]==['#AI创作','豆包AI']
