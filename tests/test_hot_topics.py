from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from backend import db, hot_topics, worker
from test_workspace import client


def douyin_body(rows):
    return {
        "code": 200,
        "data": {
            "business_data": [
                {
                    "data": {
                        "aweme_info": {
                            "aweme_id": str(row["id"]),
                            "desc": row["title"],
                            "create_time": row["created"],
                            "author": {"nickname": row["author"]},
                            "statistics": {"digg_count": row["likes"]},
                        }
                    }
                }
                for row in rows
            ]
        },
    }


def post(post_id, likes):
    return {
        "post_id": str(post_id),
        "title": "主题" + str(post_id),
        "author": "作者",
        "published_at": "2026-09-24T01:00:00+00:00",
        "likes": likes,
        "url": "https://www.douyin.com/video/" + str(post_id),
    }


def test_parse_douyin_posts_keeps_recent_top_three():
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    recent = int((now - timedelta(days=1)).timestamp())
    old = int((now - timedelta(days=8)).timestamp())
    body = douyin_body(
        [
            {"id": 1, "title": "第一", "author": "甲", "created": recent, "likes": 10},
            {"id": 2, "title": "第二", "author": "乙", "created": recent, "likes": 40},
            {"id": 3, "title": "第三", "author": "丙", "created": recent, "likes": 30},
            {"id": 4, "title": "第四", "author": "丁", "created": recent, "likes": 20},
            {"id": 5, "title": "过期", "author": "戊", "created": old, "likes": 999},
        ]
    )
    result = hot_topics.parse_douyin_posts(body, now)
    assert [item["post_id"] for item in result] == ["2", "3", "4"]
    assert result[0]["url"] == "https://www.douyin.com/video/2"


def test_parse_douyin_posts_supports_direct_and_nested_search_cards():
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    recent = int((now - timedelta(days=1)).timestamp())
    item = {
        "aweme_id": "direct-1",
        "desc": "直接结构",
        "create_time": recent,
        "author": {"nickname": "作者"},
        "statistics": {"digg_count": 12},
    }
    body = {"code": 200, "data": {"data": [
        {"type": 1, "aweme_info": item},
        {"type": 1, "aweme_info": {"card_tags": [], "payload": {"aweme_info": dict(item, aweme_id="nested-1", desc="嵌套结构")}}},
    ]}}
    assert [row["post_id"] for row in hot_topics.parse_douyin_posts(body, now)] == ["direct-1", "nested-1"]


def test_allocate_guarantees_keyword_coverage_before_twentieth_slot():
    candidates = {f"领域{i}": [post(i, i), post(100 + i, 1000 + i), post(200 + i, 2000 + i)] for i in range(1, 16)}
    result = hot_topics.allocate_posts(candidates)
    assert len(result) == 20
    covered = {keyword for item in result for keyword in item["keywords"]}
    assert covered == set(candidates)
    assert [item["rank"] for item in result] == list(range(1, 21))


def test_keyword_expansion_and_request_contract(client):
    assert hot_topics.normalize_keywords(["AI实战、Codex", "Agent;AI实战", "WorkBuddy\nSkills"]) == ["AI实战", "Codex", "Agent", "WorkBuddy", "Skills"]
    response = Mock(status_code=200)
    response.json.return_value = douyin_body([])
    with patch.object(hot_topics, "reserve_call") as budget, patch.object(hot_topics.httpx, "post", return_value=response) as request:
        hot_topics.request_douyin_posts("secret", "AI实战")
    budget.assert_called_once_with("tikhub", "douyin_hot_topics")
    assert request.call_args.kwargs["json"]["sort_type"] == "1"
    assert request.call_args.kwargs["json"]["publish_time"] == "7"
    assert request.call_args.kwargs["json"]["content_type"] == "0"
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


def test_collect_stores_all_four_platforms_and_api_has_four_tabs(client):
    with patch.object(hot_topics, "provider_secret", return_value="secret"), patch.object(
        hot_topics,
        "request_douyin_posts",
        side_effect=lambda secret, keyword: [post(keyword, len(keyword) * 100)],
    ), patch.object(
        hot_topics,
        "request_bilibili_posts",
        side_effect=lambda secret, keyword: [dict(post('b-'+keyword, len(keyword) * 90), content_type='视频')],
    ), patch.object(
        hot_topics,
        "request_zhihu_posts",
        side_effect=lambda secret, keyword: [dict(post('z-'+keyword, len(keyword) * 80), content_type='文章')],
    ), patch.object(
        hot_topics,
        "request_xiaohongshu_posts",
        side_effect=lambda secret, keyword: [dict(post('x-'+keyword, len(keyword) * 70), content_type='图文')],
    ):
        result = hot_topics.collect({"trigger": "manual"})
    assert result["keywords"] == len(db.TOPIC["keywords"])
    payload = client.get("/api/hot-topics").json()
    assert [board["platform"] for board in payload["platforms"]] == ["douyin", "bilibili", "zhihu", "xiaohongshu"]
    douyin, bilibili, zhihu, xiaohongshu = payload["platforms"]
    assert douyin["state"] == "ready" and douyin["items"]
    assert bilibili["supported"] is True and bilibili["state"] == "ready" and bilibili["items"]
    assert zhihu["supported"] is True and zhihu["state"] == "ready" and zhihu["items"]
    assert xiaohongshu["supported"] is True and xiaohongshu["state"] == "ready" and xiaohongshu["items"]


def test_latest_uses_previous_items_after_unexplained_empty_snapshot(client):
    keywords = ["AI"]
    good = {"items": [dict(post("old", 10), keywords=keywords, rank=1)], "keyword_counts": {"AI": 1}, "missing_keywords": [], "errors": {}}
    empty = {"items": [], "keyword_counts": {"AI": 0}, "missing_keywords": keywords, "errors": {}}
    with db.connect() as connection:
        connection.execute("INSERT INTO hot_topic_snapshots VALUES(?,?,?,?,?,?)", (db.uid(), "douyin", "2026-09-24T01:00:00+00:00", 1, db.dump(keywords), db.dump(good)))
        connection.execute("INSERT INTO hot_topic_snapshots VALUES(?,?,?,?,?,?)", (db.uid(), "douyin", "2026-09-24T02:00:00+00:00", 1, db.dump(keywords), db.dump(empty)))
    board = next(item for item in hot_topics.latest()["platforms"] if item["platform"] == "douyin")
    assert [item["post_id"] for item in board["items"]] == ["old"]
    assert board["updated_at"] == "2026-09-24T01:00:00+00:00"
    response = client.post("/api/topics/from-source", json={"kind": "hot_topic", "platform": "douyin", "id": "old"})
    assert response.status_code == 200


def test_bilibili_metrics_and_all_type_parser():
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    recent = int((now - timedelta(days=1)).timestamp())
    web = {"data": {"data": {"result": [{"aid": 9, "bvid": "BV9", "title": "<em>AI</em>实战", "author": "UP", "pubdate": recent, "like": 20, "review": 3, "favorites": 4, "play": 1000, "danmaku": 2}]}}}
    item = hot_topics.parse_bilibili_posts(web, now)[0]
    assert item["comments"] == 3 and item["favorites"] == 4 and item["danmaku"] == 2
    assert item["hot_score"] > item["likes"]
    all_types = {"data": {"data": {"item": [{"goto": "article", "param": "88", "article": {"id": "88", "title": "图文教程", "publish_time": recent, "author": "作者", "statistics": {"like_count": 5, "comment_count": 2, "favorite_count": 9}}}]}}}
    article = hot_topics.parse_bilibili_all_posts(all_types, now)[0]
    assert article["content_type"] == "图文/专栏" and article["favorites"] == 9
    unstable = {"data": {"data": {"item": [{"goto": "av", "av": {"aid": 10, "title": "字段类型不稳定", "ptime": recent, "author": "UP", "user_act": "unknown", "owner": "unknown"}}]}}}
    assert hot_topics.parse_bilibili_all_posts(unstable, now)[0]["likes"] == 0


def test_zhihu_parser_supports_articles_answers_and_metrics():
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    recent = int((now - timedelta(days=1)).timestamp())
    old = int((now - timedelta(days=9)).timestamp())
    body = {"data": {"data": [
        {"object": {"id": "article-1", "type": "article", "title": "<em>AI</em>文章", "created_time": recent, "voteup_count": 20, "comment_count": 3, "zfav_count": 7, "author": {"name": "甲"}}},
        {"object": {"id": "answer-1", "type": "answer", "created_time": recent, "voteup_count": 15, "comment_count": 8, "favorite_count": 4, "question": {"id": "question-1", "title": "AI回答"}, "author": {"name": "乙"}}},
        {"object": {"id": "old", "type": "article", "title": "过期文章", "created_time": old, "voteup_count": 999}},
    ]}}
    result = hot_topics.parse_zhihu_posts(body, now)
    assert {item["content_type"] for item in result} == {"文章", "回答"}
    answer = next(item for item in result if item["post_id"] == "answer-1")
    assert answer["url"] == "https://www.zhihu.com/question/question-1/answer/answer-1"
    assert answer["comments"] == 8 and answer["favorites"] == 4


def test_xiaohongshu_parser_supports_image_video_and_metrics():
    now = datetime(2026, 9, 24, 2, tzinfo=timezone.utc)
    recent = int((now - timedelta(days=1)).timestamp())
    body = {"data": {"data": {"items": [
        {"note": {"id": "image-1", "type": "normal", "title": "AI图文", "timestamp": recent, "liked_count": 30, "comments_count": 4, "collected_count": 12, "shared_count": 3, "xsec_token": "token", "user": {"nickname": "甲"}}},
        {"note": {"id": "video-1", "type": "video", "title": "AI视频", "timestamp": recent, "liked_count": 20, "comments_count": 9, "collected_count": 5, "shared_count": 6, "user": {"nickname": "乙"}}},
    ]}}}
    result = hot_topics.parse_xiaohongshu_posts(body, now)
    assert {item["content_type"] for item in result} == {"图文", "视频"}
    image = next(item for item in result if item["post_id"] == "image-1")
    assert image["comments"] == 4 and image["favorites"] == 12 and image["shares"] == 3
    assert "xsec_token=token" in image["url"]


def test_zhihu_and_xiaohongshu_request_contracts():
    response = Mock(status_code=200)
    response.json.return_value = {"code": 200, "data": {"data": {"items": []}}}
    with patch.object(hot_topics, "reserve_call") as budget, patch.object(hot_topics.httpx, "get", return_value=response) as request:
        hot_topics.request_xiaohongshu_posts("secret", "AI实战")
    budget.assert_called_once_with("tikhub", "xiaohongshu_hot_topics")
    assert request.call_args.kwargs["params"]["note_type"] == "不限"
    assert request.call_args.kwargs["params"]["time_filter"] == "一周内"
    assert request.call_args.kwargs["params"]["sort_type"] == "popularity_descending"
    response.json.return_value = {"code": 200, "data": {"data": []}}
    with patch.object(hot_topics, "reserve_call") as budget, patch.object(hot_topics.httpx, "get", return_value=response) as request:
        hot_topics.request_zhihu_posts("secret", "AI实战")
    budget.assert_called_once_with("tikhub", "zhihu_hot_topics")
    assert request.call_args.kwargs["params"]["vertical"] == ""
    assert request.call_args.kwargs["params"]["sort"] == "upvoted_count"
    assert request.call_args.kwargs["params"]["time_interval"] == "a_week"


def test_hot_topic_can_be_added_to_topic_pool(client):
    with db.connect() as connection:
        payload = {"items": [dict(post("chosen", 100), keywords=["AI实战"], comments=2, favorites=3, shares=1, hot_score=113)], "keyword_counts": {"AI实战": 1}, "missing_keywords": [], "errors": {}}
        connection.execute("INSERT INTO hot_topic_snapshots VALUES(?,?,?,?,?,?)", (db.uid(), "douyin", db.now(), 1, db.dump(["AI实战"]), db.dump(payload)))
    response = client.post("/api/topics/from-source", json={"kind": "hot_topic", "platform": "douyin", "id": "chosen"})
    assert response.status_code == 200
    assert client.get("/api/hot-topics").json()["platforms"][0]["items"][0]["in_topic_pool"] is True
    assert client.post("/api/topics/from-source", json={"kind": "hot_topic", "platform": "douyin", "id": "chosen"}).json()["duplicate"] is True


def test_manual_refresh_is_exclusive(client):
    worker.worker.stop()
    first = client.post("/api/hot-topics/refresh", json={})
    second = client.post("/api/hot-topics/refresh", json={})
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "hot_topics_busy"
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs WHERE type='hot_topics'").fetchone()[0] == 1


def test_refresh_waits_when_budget_cannot_complete_all_platforms(client):
    worker.worker.stop()
    with db.connect() as connection:
        connection.execute("INSERT INTO provider_budgets VALUES('tikhub',0)")
        credential = db.uid()
        connection.execute("INSERT INTO credentials VALUES(?,?,?,?,?)", (credential, "tikhub", b"", "••••test", db.now()))
        connection.execute("INSERT INTO provider_settings VALUES(?,?,?,?)", ("tikhub", credential, "verified", db.now()))
    response = client.post("/api/hot-topics/refresh", json={})
    assert response.status_code == 400 and "完整更新四个平台需要" in response.json()["message"]
    assert hot_topics.schedule_once(datetime(2026, 9, 24, 9, 1, tzinfo=hot_topics.SHANGHAI)) is None
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs WHERE type='hot_topics'").fetchone()[0] == 0


def test_scheduler_catches_up_after_nine_without_duplicate_job(client):
    worker.worker.stop()
    hot_topics.scheduler.stop()
    with db.connect() as connection:
        credential = db.uid()
        connection.execute("INSERT INTO credentials VALUES(?,?,?,?,?)", (credential, "tikhub", b"", "••••test", db.now()))
        connection.execute("INSERT INTO provider_settings VALUES(?,?,?,?)", ("tikhub", credential, "verified", db.now()))
    now = datetime(2026, 9, 24, 9, 1, tzinfo=hot_topics.SHANGHAI)
    first = hot_topics.schedule_once(now)
    second = hot_topics.schedule_once(now)
    assert first and first["state"] == "queued"
    assert second and second["busy"] is True
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs WHERE type='hot_topics'").fetchone()[0] == 1
