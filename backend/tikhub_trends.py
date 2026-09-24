"""TikHub search-hot-word feeds, excluding posts, topic feeds and suggestions."""
from urllib.parse import quote
import httpx


BASE='https://api.tikhub.io/api/v1'
FEEDS={
    'douyin':('POST','/douyin/billboard/fetch_hot_total_search_list'),
    'bilibili':('GET','/bilibili/web/fetch_hot_search'),
    'zhihu':('GET','/zhihu/web/fetch_search_recommend'),
}


def parse_hot_words(platform,body):
    from .providers import ProviderError
    if not isinstance(body,dict) or body.get('code')!=200 or not isinstance(body.get('data'),dict):
        raise ProviderError('TikHub '+platform+' 热搜词请求未成功或字段不符')
    data=body['data']
    if platform=='douyin':
        rows=data.get('data',{}).get('search_list') if isinstance(data.get('data'),dict) else None
        if not isinstance(rows,list):raise ProviderError('TikHub 抖音搜索热榜字段不符')
        words=[dict(title=x['key_word'],index=i+1,hotCount=x.get('search_score'),url='https://www.douyin.com/search/'+quote(x['key_word']),gmtCreate=body.get('time')) for i,x in enumerate(rows) if isinstance(x,dict) and isinstance(x.get('key_word'),str) and x['key_word'].strip()]
    elif platform=='bilibili':
        nested=data.get('data',{})
        trending=nested.get('trending',{}) if isinstance(nested,dict) else {}
        rows=trending.get('list') if isinstance(trending,dict) else None
        if not isinstance(rows,list):raise ProviderError('TikHub B站热搜关键词字段不符')
        words=[dict(title=x['keyword'],index=i+1,hotCount=x.get('heat'),url=None,gmtCreate=body.get('time')) for i,x in enumerate(rows) if isinstance(x,dict) and isinstance(x.get('keyword'),str) and x['keyword'].strip()]
    elif platform=='zhihu':
        recommend=data.get('recommend_queries',{})
        rows=recommend.get('queries') if isinstance(recommend,dict) else None
        if not isinstance(rows,list):raise ProviderError('TikHub 知乎搜索热词字段不符')
        words=[dict(title=x.get('real_query') or x.get('query'),index=i+1,hotCount=None,url=None,gmtCreate=body.get('time')) for i,x in enumerate(rows) if isinstance(x,dict) and x.get('label')=='hot' and isinstance(x.get('real_query') or x.get('query'),str) and (x.get('real_query') or x.get('query')).strip()]
    else:raise ValueError('不支持的热搜词平台')
    return words


def request_hot_words(secret,platform,limit=50):
    from .providers import ProviderError,reserve_call
    if platform not in FEEDS:raise ValueError('不支持的热搜词平台')
    method,path=FEEDS[platform]
    reserve_call('tikhub',platform+'_hot_words')
    params={'page_num':1,'page_size':limit,'date_window':24,'keyword':'AI'} if platform=='douyin' else {'limit':limit} if platform=='bilibili' else {}
    try:
        response=httpx.request(method,BASE+path,headers={'Authorization':'Bearer '+secret},json=params if method=='POST' else None,params=params if method=='GET' else None,timeout=30,follow_redirects=False)
        if response.status_code!=200:raise ProviderError('TikHub '+platform+f' 热搜词请求失败（HTTP {response.status_code}）')
        return parse_hot_words(platform,response.json())
    except httpx.HTTPError:raise ProviderError('TikHub '+platform+' 热搜词网络请求失败；未自动重复调用') from None
