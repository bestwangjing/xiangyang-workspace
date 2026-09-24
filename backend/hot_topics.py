"""Keyword-driven hot-topic collection for the four configured platforms."""
from __future__ import annotations

import html
import re
import threading
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .providers import ProviderError, provider_secret, reserve_call

SHANGHAI = timezone(timedelta(hours=8))
DOUYIN_URL = "https://api.tikhub.io/api/v1/douyin/search/fetch_general_search_v1"
BILIBILI_WEB_URL = "https://api.tikhub.io/api/v1/bilibili/web/fetch_general_search"
BILIBILI_ALL_URL = "https://api.tikhub.io/api/v1/bilibili/app/fetch_search_all"
ZHIHU_URL = "https://api.tikhub.io/api/v1/zhihu/web/fetch_article_search_v3"
XIAOHONGSHU_URL = "https://api.tikhub.io/api/v1/xiaohongshu/app_v2/search_notes"
ALL_PLATFORMS = ("douyin", "bilibili", "zhihu", "xiaohongshu")
SUPPORTED = ALL_PLATFORMS
CALLS_PER_KEYWORD = {"douyin": 1, "bilibili": 2, "zhihu": 1, "xiaohongshu": 1}


def normalize_keywords(values):
    result = []
    for value in values or []:
        for part in re.split(r"[\s,，、;；|]+", str(value or "")):
            part = part.strip()
            if part and part.casefold() not in {item.casefold() for item in result}:
                result.append(part)
    return result[:20]


def _number(value):
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value or "").strip().replace(",", "").lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万|w|千|k|亿)?", raw)
    if not match:
        return 0
    return int(float(match.group(1)) * {None: 1, "万": 10000, "w": 10000, "千": 1000, "k": 1000, "亿": 100000000}[match.group(2)])


def _text(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(str(value or "")))).strip()


def _dict(value):
    return value if isinstance(value, dict) else {}


def _timestamp(value):
    try:
        number = int(value)
        if number > 10**12:
            number //= 1000
        return datetime.fromtimestamp(number, timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _recent(moment, now):
    return moment is not None and now - timedelta(days=7) <= moment <= now + timedelta(hours=1)


def _heat(likes=0, comments=0, favorites=0, shares=0, views=0, danmaku=0):
    # Engagement is more intentional than a passive view; weights are only used
    # to rank results within the same platform and keyword.
    return int(likes + comments * 2 + favorites * 2 + shares * 3 + views * 0.02 + danmaku * 0.5)


def _body(response, platform):
    if response.status_code != 200:
        raise ProviderError(f"TikHub {platform}搜索失败（HTTP {response.status_code}）")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ProviderError(f"TikHub {platform}搜索返回格式异常")
    if payload.get("code") != 200:
        raise ProviderError(f"TikHub {platform}搜索未成功：{payload.get('message_zh') or payload.get('message') or '返回异常'}")
    return payload


def _douyin_rows(body):
    data = _dict(body.get("data"))
    for rows in (data.get("business_data"), data.get("data"), body.get("business_data")):
        if isinstance(rows, list):
            return rows
    return []


def _douyin_item(row):
    """Unwrap both historical and current TikHub Douyin search cards."""
    queue = [_dict(row.get("data")), _dict(row.get("aweme_info")), row]
    seen = set()
    while queue:
        candidate = queue.pop(0)
        if not isinstance(candidate, dict) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if (candidate.get("aweme_id") or candidate.get("group_id")) and candidate.get("create_time") and (candidate.get("desc") or candidate.get("title")):
            return candidate
        # TikHub has returned aweme_info as either the work itself or another
        # search-card wrapper. Prefer explicitly named children, then inspect
        # the remaining nested dictionaries for forward-compatible unwrapping.
        nested = _dict(candidate.get("aweme_info"))
        if nested:
            queue.insert(0, nested)
        queue.extend(value for value in candidate.values() if isinstance(value, dict) and value is not nested)
    return {}


def parse_douyin_posts(body, now=None):
    now = now or datetime.now(timezone.utc)
    result = []
    for row in _douyin_rows(body):
        if not isinstance(row, dict):
            continue
        item = _douyin_item(row)
        post_id = str(item.get("aweme_id") or item.get("group_id") or "").strip()
        title = _text(item.get("desc") or item.get("title"))
        published = _timestamp(item.get("create_time"))
        if not post_id or not title or not _recent(published, now):
            continue
        stats = _dict(item.get("statistics"))
        author = _dict(item.get("author"))
        likes = _number(stats.get("digg_count"))
        comments = _number(stats.get("comment_count"))
        favorites = _number(stats.get("collect_count"))
        shares = _number(stats.get("share_count"))
        images = item.get("images") or item.get("image_infos") or item.get("image_post_info")
        result.append(dict(
            post_id=post_id, title=title, author=_text(author.get("nickname") or author.get("unique_id")) or "未知作者",
            published_at=published.isoformat(), likes=likes, comments=comments, favorites=favorites, shares=shares,
            views=_number(stats.get("play_count")), danmaku=0,
            content_type="图文" if images or item.get("aweme_type") == 68 else "视频",
            hot_score=_heat(likes, comments, favorites, shares), ranking_basis="互动热度",
            url=(item.get("share_url") or _dict(item.get("share_info")).get("share_url") or (f"https://www.douyin.com/note/{post_id}" if images or item.get("aweme_type") == 68 else f"https://www.douyin.com/video/{post_id}")),
        ))
    return sorted(result, key=lambda item: (item["hot_score"], item["likes"]), reverse=True)[:3]


def request_douyin_posts(secret, keyword):
    reserve_call("tikhub", "douyin_hot_topics")
    try:
        response = httpx.post(
            DOUYIN_URL,
            headers={"Authorization": "Bearer " + secret},
            json=dict(keyword=keyword, cursor=0, sort_type="1", publish_time="7", filter_duration="0", content_type="0", search_id="", backtrace=""),
            timeout=45,
            follow_redirects=False,
        )
        return parse_douyin_posts(_body(response, "抖音"))
    except httpx.HTTPError:
        raise ProviderError("TikHub 抖音搜索网络请求失败；未自动重复调用") from None


def parse_bilibili_posts(body, now=None):
    now = now or datetime.now(timezone.utc)
    data = _dict(body.get("data"))
    if isinstance(data.get("data"), dict):
        data = data["data"]
    rows = data.get("result") or []
    result = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        published = _timestamp(item.get("pubdate") or item.get("senddate") or item.get("ptime"))
        if not _recent(published, now):
            continue
        post_id = str(item.get("aid") or item.get("bvid") or item.get("id") or "").strip()
        title = _text(item.get("title"))
        if not post_id or not title:
            continue
        likes = _number(item.get("like"))
        comments = _number(item.get("review") or item.get("comment"))
        favorites = _number(item.get("favorites") or item.get("stow"))
        views = _number(item.get("play") or item.get("view"))
        danmaku = _number(item.get("danmaku") or item.get("video_review"))
        bvid = item.get("bvid")
        result.append(dict(
            post_id=post_id, title=title, author=_text(item.get("author") or item.get("up_name")) or "未知作者",
            published_at=published.isoformat(), likes=likes, comments=comments, favorites=favorites, shares=0,
            views=views, danmaku=danmaku, content_type="视频",
            hot_score=_heat(likes, comments, favorites, views=views, danmaku=danmaku), ranking_basis="互动热度",
            url=item.get("arcurl") or (f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{post_id}"),
        ))
    return result


def parse_bilibili_all_posts(body, now=None):
    """Parse all Bilibili search types when the app response supplies usable metadata."""
    now = now or datetime.now(timezone.utc)
    data = _dict(body.get("data"))
    if isinstance(data.get("data"), dict):
        data = data["data"]
    result = []
    for row in data.get("item") or []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("goto") or row.get("type") or "").lower()
        nested = row.get("av") or row.get("article") or row.get("dynamic") or row.get("opus") or row
        if not isinstance(nested, dict):
            continue
        published = _timestamp(nested.get("ptime") or nested.get("pubdate") or nested.get("publish_time") or row.get("ptime"))
        # TikHub's app result sometimes emits navigation-only article stubs.
        # They cannot safely satisfy the seven-day rule and are deliberately skipped.
        if not _recent(published, now):
            continue
        post_id = str(nested.get("aid") or nested.get("id") or row.get("param") or "").strip()
        title = _text(nested.get("title") or nested.get("desc") or nested.get("content"))
        if not post_id or not title:
            continue
        stats = _dict(nested.get("stat")) or _dict(nested.get("statistics")) or _dict(nested.get("user_act")) or _dict(row.get("user_act"))
        likes = _number(stats.get("like") or stats.get("like_count"))
        comments = _number(stats.get("reply") or stats.get("comment") or stats.get("comment_count"))
        favorites = _number(stats.get("favorite") or stats.get("collect") or stats.get("favorite_count"))
        shares = _number(stats.get("share") or stats.get("forward"))
        views = _number(nested.get("play") or stats.get("view"))
        danmaku = _number(nested.get("danmaku") or stats.get("danmaku"))
        is_video = kind in {"av", "video"} or bool(nested.get("aid") or nested.get("bvid"))
        uri = row.get("uri") or nested.get("url")
        result.append(dict(
            post_id=post_id, title=title,
            author=_text(nested.get("author") or nested.get("up_name") or _dict(nested.get("owner")).get("name")) or "未知作者",
            published_at=published.isoformat(), likes=likes, comments=comments, favorites=favorites, shares=shares,
            views=views, danmaku=danmaku, content_type="视频" if is_video else "图文/专栏",
            hot_score=_heat(likes, comments, favorites, shares, views, danmaku), ranking_basis="互动热度",
            url=uri if str(uri or "").startswith("http") else (f"https://www.bilibili.com/video/av{post_id}" if is_video else f"https://www.bilibili.com/opus/{post_id}"),
        ))
    return result


def _dedupe_top(items):
    merged = {}
    for item in items:
        key = str(item["post_id"])
        if key not in merged or item["hot_score"] > merged[key]["hot_score"]:
            merged[key] = item
    return sorted(merged.values(), key=lambda item: (item["hot_score"], item["likes"], item["favorites"]), reverse=True)[:3]


def request_bilibili_posts(secret, keyword):
    headers = {"Authorization": "Bearer " + secret}
    now = datetime.now(timezone.utc)
    begin = int((now - timedelta(days=7)).timestamp())
    end = int(now.timestamp())
    try:
        reserve_call("tikhub", "bilibili_hot_topics")
        web = httpx.get(BILIBILI_WEB_URL, headers=headers, params=dict(keyword=keyword, order="totalrank", page=1, page_size=42, duration=0, pubtime_begin_s=begin, pubtime_end_s=end), timeout=45, follow_redirects=False)
        reserve_call("tikhub", "bilibili_hot_topics_all_types")
        all_types = httpx.get(BILIBILI_ALL_URL, headers=headers, params=dict(keyword=keyword, cursor="", page_size=30, order=0), timeout=45, follow_redirects=False)
        return _dedupe_top(parse_bilibili_posts(_body(web, "B站"), now) + parse_bilibili_all_posts(_body(all_types, "B站"), now))
    except httpx.HTTPError:
        raise ProviderError("TikHub B站搜索网络请求失败；未自动重复调用") from None


def _zhihu_url(item, post_id, kind):
    raw = str(item.get("url") or "")
    if kind == "article":
        return f"https://zhuanlan.zhihu.com/p/{post_id}"
    if kind in {"zvideo", "video"}:
        return f"https://www.zhihu.com/zvideo/{post_id}"
    question = _dict(item.get("question"))
    question_id = question.get("id") or item.get("question_id")
    if kind == "answer" and question_id:
        return f"https://www.zhihu.com/question/{question_id}/answer/{post_id}"
    if raw.startswith("https://www.zhihu.com") or raw.startswith("https://zhuanlan.zhihu.com"):
        return raw
    return f"https://www.zhihu.com/search?q={post_id}"


def parse_zhihu_posts(body, now=None):
    now = now or datetime.now(timezone.utc)
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        rows = data["data"]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    result = []
    labels = {"article": "文章", "answer": "回答", "zvideo": "视频", "video": "视频", "question": "问题"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _dict(row.get("object")) or row
        kind = str(item.get("type") or row.get("type") or "article").lower()
        post_id = str(item.get("id") or "").strip()
        question = _dict(item.get("question"))
        highlight = _dict(row.get("highlight"))
        title = _text(item.get("title") or question.get("title") or highlight.get("title"))
        published = _timestamp(item.get("created_time") or item.get("published_time") or item.get("created_at"))
        if not post_id or not title or not _recent(published, now):
            continue
        author = _dict(item.get("author"))
        likes = _number(item.get("voteup_count") or item.get("upvote_count"))
        comments = _number(item.get("comment_count") or item.get("comments_count"))
        favorites = _number(item.get("zfav_count") or item.get("favorite_count") or item.get("favlists_count"))
        views = _number(item.get("view_count") or item.get("read_count") or item.get("play_count"))
        result.append(dict(
            post_id=post_id, title=title, author=_text(author.get("name")) or "未知作者",
            published_at=published.isoformat(), likes=likes, comments=comments, favorites=favorites,
            shares=None, views=views if views else None, danmaku=0, content_type=labels.get(kind, "内容"),
            hot_score=_heat(likes, comments, favorites, views=views), ranking_basis="互动热度",
            url=_zhihu_url(item, post_id, kind),
        ))
    return _dedupe_top(result)


def request_zhihu_posts(secret, keyword):
    reserve_call("tikhub", "zhihu_hot_topics")
    try:
        response = httpx.get(
            ZHIHU_URL,
            headers={"Authorization": "Bearer " + secret},
            params=dict(keyword=keyword, offset=0, limit=20, show_all_topics=0, search_source="Filter", search_hash_id="", vertical="", sort="upvoted_count", time_interval="a_week"),
            timeout=45,
            follow_redirects=False,
        )
        return parse_zhihu_posts(_body(response, "知乎"))
    except httpx.HTTPError:
        raise ProviderError("TikHub 知乎搜索网络请求失败；未自动重复调用") from None


def parse_xiaohongshu_posts(body, now=None):
    now = now or datetime.now(timezone.utc)
    data = _dict(body.get("data"))
    if isinstance(data.get("data"), dict):
        data = data["data"]
    rows = data.get("items") or []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _dict(row.get("note")) or row
        post_id = str(item.get("id") or item.get("note_id") or "").strip()
        title = _text(item.get("title")) or _text(item.get("desc"))[:100]
        published = _timestamp(item.get("timestamp") or item.get("publish_time") or item.get("update_time"))
        if not post_id or not title or not _recent(published, now):
            continue
        author = _dict(item.get("user"))
        likes = _number(item.get("liked_count") or item.get("nice_count") or item.get("likes"))
        comments = _number(item.get("comments_count") or item.get("comment_count"))
        favorites = _number(item.get("collected_count") or item.get("collect_count"))
        shares = _number(item.get("shared_count") or item.get("share_count"))
        note_type = str(item.get("type") or item.get("note_type") or "normal").lower()
        token = item.get("xsec_token") or row.get("xsec_token")
        url = f"https://www.xiaohongshu.com/explore/{post_id}"
        if token:
            from urllib.parse import urlencode
            url += "?" + urlencode({"xsec_token": token, "xsec_source": "pc_search"})
        result.append(dict(
            post_id=post_id, title=title, author=_text(author.get("nickname") or author.get("name")) or "未知作者",
            published_at=published.isoformat(), likes=likes, comments=comments, favorites=favorites, shares=shares,
            views=None, danmaku=0, content_type="视频" if note_type == "video" else "图文",
            hot_score=_heat(likes, comments, favorites, shares), ranking_basis="互动热度", url=url,
        ))
    return _dedupe_top(result)


def request_xiaohongshu_posts(secret, keyword):
    reserve_call("tikhub", "xiaohongshu_hot_topics")
    try:
        response = httpx.get(
            XIAOHONGSHU_URL,
            headers={"Authorization": "Bearer " + secret},
            params=dict(keyword=keyword, page=1, sort_type="popularity_descending", note_type="不限", time_filter="一周内", search_id="", search_session_id="", source="explore_feed", ai_mode=0),
            timeout=45,
            follow_redirects=False,
        )
        return parse_xiaohongshu_posts(_body(response, "小红书"))
    except httpx.HTTPError:
        raise ProviderError("TikHub 小红书搜索网络请求失败；未自动重复调用") from None


def allocate_posts(candidates, limit=20):
    chosen = []
    by_id = {}

    def add(keyword, item):
        key = str(item["post_id"])
        if key in by_id:
            if keyword not in by_id[key]["keywords"]:
                by_id[key]["keywords"].append(keyword)
            return False
        if len(chosen) >= limit:
            return False
        row = dict(item, keywords=[keyword])
        chosen.append(row)
        by_id[key] = row
        return True

    # Coverage pass: reserve the first available item for every configured field.
    for keyword, items in candidates.items():
        if items:
            add(keyword, items[0])
    remaining = sorted(
        ((keyword, item) for keyword, items in candidates.items() for item in items),
        key=lambda pair: (pair[1].get("hot_score", pair[1].get("likes", 0)), pair[1].get("likes", 0)),
        reverse=True,
    )
    for keyword, item in remaining:
        add(keyword, item)
        if len(chosen) >= limit:
            break
    chosen.sort(key=lambda item: (item.get("hot_score", item.get("likes", 0)), item.get("likes", 0)), reverse=True)
    for index, item in enumerate(chosen, 1):
        item["rank"] = index
    return chosen


def _save_snapshot(platform, keywords, candidates, errors, profile_version):
    items = allocate_posts(candidates)
    payload = dict(
        items=items,
        keyword_counts={keyword: len(candidates.get(keyword, [])) for keyword in keywords},
        missing_keywords=[keyword for keyword in keywords if not candidates.get(keyword)],
        errors=errors,
    )
    with db.connect() as connection:
        # A successful upstream response can still contain an unsupported card
        # wrapper. Do not replace a usable board with an unexplained empty one.
        if not items and not errors:
            previous = db.one(connection, "SELECT payload FROM hot_topic_snapshots WHERE platform=? ORDER BY collected_at DESC", (platform,))
            if previous:
                previous_payload = db.load(previous["payload"])
                if previous_payload.get("items"):
                    return previous_payload
        connection.execute("INSERT INTO hot_topic_snapshots VALUES(?,?,?,?,?,?)", (db.uid(), platform, db.now(), profile_version, db.dump(keywords), db.dump(payload)))
    return payload


def collect(payload):
    secret = provider_secret("tikhub")
    with db.connect() as connection:
        snapshot = db.snapshot(connection)
        recent = {}
        if payload.get("trigger") == "scheduled":
            for row in db.rows(connection, "SELECT platform,max(collected_at) AS collected_at FROM hot_topic_snapshots WHERE platform IN ('douyin','bilibili','zhihu','xiaohongshu') GROUP BY platform"):
                if datetime.fromisoformat(row["collected_at"]).astimezone(SHANGHAI).date() >= datetime.now(SHANGHAI).date():
                    recent[row["platform"]] = db.one(connection, "SELECT payload FROM hot_topic_snapshots WHERE platform=? ORDER BY collected_at DESC LIMIT 1", (row["platform"],))
    keywords = normalize_keywords(snapshot["rules"].get("keywords"))
    if not keywords:
        raise ValueError("请先在账号画像中配置关注领域关键词")
    requests = {"douyin": request_douyin_posts, "bilibili": request_bilibili_posts, "zhihu": request_zhihu_posts, "xiaohongshu": request_xiaohongshu_posts}
    totals = {}
    all_errors = {}
    for platform in SUPPORTED:
        if platform in recent:
            totals[platform] = len(db.load(recent[platform]["payload"]).get("items", []))
            continue
        candidates = {}
        errors = {}
        for keyword in keywords:
            try:
                candidates[keyword] = requests[platform](secret, keyword)
            except ProviderError as exc:
                candidates[keyword] = []
                errors[keyword] = str(exc)
        saved = _save_snapshot(platform, keywords, candidates, errors, snapshot["profile_version"])
        totals[platform] = len(saved["items"])
        if errors:
            all_errors[platform] = errors
    return dict(trigger=payload.get("trigger", "manual"), keywords=len(keywords), counts=totals, errors=all_errors)


def _topic_pool_keys(connection):
    keys = set()
    for row in db.rows(connection, "SELECT payload FROM topic_candidates WHERE status!='disliked'"):
        try:
            key = db.load(row["payload"]).get("source_key")
            if key:
                keys.add(key)
        except (TypeError, ValueError):
            pass
    return keys


def latest():
    platforms = []
    with db.connect() as connection:
        pool = _topic_pool_keys(connection)
        for platform in ALL_PLATFORMS:
            rows = db.rows(connection, "SELECT * FROM hot_topic_snapshots WHERE platform=? ORDER BY collected_at DESC LIMIT 20", (platform,))
            row = rows[0] if rows else None
            # Compatibility fallback for an already-saved unexplained empty
            # snapshot: show the most recent usable snapshot instead.
            if row:
                newest_payload = db.load(row["payload"])
                if not newest_payload.get("items") and not newest_payload.get("errors"):
                    row = next((candidate for candidate in rows[1:] if db.load(candidate["payload"]).get("items")), row)
            if row:
                payload = db.load(row["payload"])
                items = [dict(item, in_topic_pool=f"hot_topic:{platform}:{item['post_id']}" in pool) for item in payload.get("items", [])]
                keywords = db.load(row["keywords"])
                keyword_counts = payload.get("keyword_counts")
                if not isinstance(keyword_counts, dict) or not keyword_counts:
                    keyword_counts = {keyword: sum(keyword in item.get("keywords", []) for item in items) for keyword in keywords}
                platforms.append(dict(platform=platform, supported=platform in SUPPORTED, state="ready", updated_at=row["collected_at"], keywords=keywords, items=items, keyword_counts=keyword_counts, missing_keywords=payload.get("missing_keywords", []), errors=payload.get("errors", {})))
            else:
                platforms.append(dict(platform=platform, supported=platform in SUPPORTED, state="empty" if platform in SUPPORTED else "coming_soon", updated_at=None, keywords=[], keyword_counts={}, items=[], missing_keywords=[], errors={}))
        active = db.one(connection, "SELECT id,state FROM jobs WHERE type='hot_topics' AND state IN ('queued','running','waiting_auth','waiting_quota','recovery_required') ORDER BY created_at DESC LIMIT 1")
    return dict(platforms=platforms, active_job=active)


def enqueue_refresh(trigger="manual"):
    from .worker import enqueue
    if trigger == "manual":
        with db.connect() as connection:
            keywords = normalize_keywords(db.snapshot(connection)["rules"].get("keywords"))
            available = _available_calls(connection)
        required = len(keywords) * sum(CALLS_PER_KEYWORD.values())
        if available < required:
            raise ValueError(f"TikHub 今日剩余调用额度为 {available} 次，完整更新四个平台需要 {required} 次；请调整每日上限或等待次日自动更新。")
    key = f"hot-topics:{trigger}:{db.uid()}" if trigger == "manual" else f"hot-topics:scheduled:{datetime.now(SHANGHAI).date().isoformat()}"
    return enqueue("hot_topics", {"trigger": trigger}, key=key, exclusive=True)


def _available_calls(connection):
    today = datetime.now(SHANGHAI).date().isoformat()
    budget = db.one(connection, "SELECT daily_call_limit FROM provider_budgets WHERE provider='tikhub'")
    limit = budget["daily_call_limit"] if budget else 50
    used = connection.execute("SELECT count(*) FROM provider_calls WHERE provider='tikhub' AND local_day=?", (today,)).fetchone()[0]
    return max(0, limit - used)


def schedule_once(now=None):
    now = now or datetime.now(SHANGHAI)
    if now.hour < 9:
        return None
    with db.connect() as connection:
        configured = db.one(connection, "SELECT 1 FROM provider_settings WHERE provider='tikhub' AND status='verified'")
        latest_rows = db.rows(connection, "SELECT platform,max(collected_at) AS collected_at FROM hot_topic_snapshots WHERE platform IN ('douyin','bilibili','zhihu','xiaohongshu') GROUP BY platform")
        keywords = normalize_keywords(db.snapshot(connection)["rules"].get("keywords"))
        available = _available_calls(connection)
    if not configured:
        return None
    collected = {row["platform"]: datetime.fromisoformat(row["collected_at"]).astimezone(SHANGHAI).date() for row in latest_rows}
    if all(collected.get(platform) and collected[platform] >= now.date() for platform in SUPPORTED):
        return None
    stale = [platform for platform in SUPPORTED if not collected.get(platform) or collected[platform] < now.date()]
    required = len(keywords) * sum(CALLS_PER_KEYWORD[platform] for platform in stale)
    if available < required:
        return None
    return enqueue_refresh("scheduled")


class Scheduler:
    def __init__(self):
        self.event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.event.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="hot-topic-scheduler")
        self.thread.start()

    def stop(self):
        self.event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def loop(self):
        while not self.event.is_set():
            try:
                schedule_once()
            except Exception:
                pass
            self.event.wait(60)


scheduler = Scheduler()
