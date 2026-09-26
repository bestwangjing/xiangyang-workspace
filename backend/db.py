"""Disk-only store. Every mutable business operation uses a short transaction."""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Data root: XIANGYANG_DATA_ROOT wins; the portable default keeps the project
# runnable on machines without a D: drive. start-workspace.ps1 sets the env var
# from .data-root.local when a machine-specific location is needed.
ROOT = Path(os.environ.get('XIANGYANG_DATA_ROOT') or os.path.join(os.environ.get('LOCALAPPDATA', str(Path.home())), 'XiangyangWorkspace'))

def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return str(uuid.uuid4())
def dump(value): return json.dumps(value, ensure_ascii=False, default=str)
def load(value): return json.loads(value)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS screenshot_updates(evidence_id TEXT PRIMARY KEY REFERENCES evidence(id), platform TEXT NOT NULL, proposal TEXT NOT NULL, outcome TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS screenshot_batch_items(job_id TEXT NOT NULL REFERENCES jobs(id), evidence_id TEXT NOT NULL REFERENCES evidence(id), state TEXT NOT NULL, stage TEXT NOT NULL, result TEXT, seconds REAL, updated_at TEXT NOT NULL, PRIMARY KEY(job_id,evidence_id));
CREATE TABLE IF NOT EXISTS trend_relevance(snapshot_id TEXT NOT NULL,context_hash TEXT NOT NULL,payload TEXT NOT NULL,config_version_id TEXT NOT NULL,checked_at TEXT NOT NULL,PRIMARY KEY(snapshot_id,context_hash));
CREATE TABLE IF NOT EXISTS provider_validation(credential_ref TEXT PRIMARY KEY,status TEXT NOT NULL,error TEXT,checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, platform TEXT UNIQUE NOT NULL, nickname TEXT NOT NULL, platform_account_id TEXT, homepage TEXT, sec_uid TEXT);
CREATE TABLE IF NOT EXISTS profile_versions(version INTEGER PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rule_versions(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(kind,version));
CREATE TABLE IF NOT EXISTS workspace_settings(id INTEGER PRIMARY KEY CHECK(id=1), profile_version INTEGER REFERENCES profile_versions(version), topic_version INTEGER NOT NULL, review_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS contents(id TEXT PRIMARY KEY, kind TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, source_created_raw TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS content_revisions(id TEXT PRIMARY KEY, content_id TEXT NOT NULL REFERENCES contents(id), revision INTEGER NOT NULL, title TEXT NOT NULL, body_text TEXT NOT NULL, source_hash TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(content_id,revision));
CREATE TABLE IF NOT EXISTS source_notes(id TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL, content_id TEXT REFERENCES contents(id), source_hash TEXT, fingerprint TEXT, imported_revision INTEGER, last_seen_at TEXT, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_assets(id TEXT PRIMARY KEY, content_id TEXT REFERENCES contents(id), relative_path TEXT UNIQUE NOT NULL, source_path TEXT, sha256 TEXT NOT NULL, mime TEXT, size INTEGER, role TEXT, sort_order INTEGER, availability TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, type TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0, config_version_id TEXT, active_revision INTEGER, next_retry_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_items(job_id TEXT REFERENCES jobs(id), note_id TEXT NOT NULL, state TEXT NOT NULL, result TEXT, PRIMARY KEY(job_id,note_id));
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, kind TEXT NOT NULL, relative_path TEXT UNIQUE NOT NULL, sha256 TEXT UNIQUE NOT NULL, captured_at TEXT, received_at TEXT NOT NULL, ocr_json TEXT, parser_json TEXT, inbox_cleared_at TEXT);
CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY, content_id TEXT REFERENCES contents(id), account_id TEXT NOT NULL REFERENCES accounts(id), platform TEXT NOT NULL, platform_post_id TEXT, canonical_url TEXT, status TEXT NOT NULL, title_override TEXT, cover_url TEXT, platform_kind TEXT, published_local_date TEXT, published_at TEXT, published_precision TEXT NOT NULL DEFAULT 'unknown', published_time_status TEXT NOT NULL DEFAULT 'unknown', published_evidence_id TEXT REFERENCES evidence(id), version INTEGER NOT NULL DEFAULT 1, UNIQUE(platform,platform_post_id));
CREATE UNIQUE INDEX IF NOT EXISTS unique_publication_url ON publications(canonical_url) WHERE canonical_url IS NOT NULL;
CREATE TABLE IF NOT EXISTS publication_time_history(id TEXT PRIMARY KEY, publication_id TEXT REFERENCES publications(id), evidence_id TEXT REFERENCES evidence(id), old_value TEXT, new_value TEXT, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_details(publication_id TEXT PRIMARY KEY REFERENCES publications(id) ON DELETE CASCADE, title TEXT, body_text TEXT, cover_url TEXT, media_json TEXT NOT NULL DEFAULT '[]', fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metric_snapshots(id TEXT PRIMARY KEY, publication_id TEXT REFERENCES publications(id), account_id TEXT REFERENCES accounts(id), evidence_id TEXT REFERENCES evidence(id), source TEXT NOT NULL, observed_at TEXT, window_start TEXT, window_end TEXT, scope TEXT NOT NULL, dedupe_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, CHECK((publication_id IS NULL)!=(account_id IS NULL)));
CREATE TABLE IF NOT EXISTS metric_values(snapshot_id TEXT REFERENCES metric_snapshots(id), metric_key TEXT, value REAL, unit TEXT, estimated INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(snapshot_id,metric_key));
CREATE TABLE IF NOT EXISTS metric_corrections(id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES metric_snapshots(id), metric_key TEXT NOT NULL, old_value REAL, new_value REAL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publication_overrides(publication_id TEXT PRIMARY KEY REFERENCES publications(id), title TEXT, body_text TEXT, cover_asset_id TEXT REFERENCES media_assets(id), paid_status TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE IF NOT EXISTS reference_preferences(reference_id TEXT PRIMARY KEY REFERENCES reference_posts(id), saved INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS reference_pending(id TEXT PRIMARY KEY, title TEXT, reason TEXT NOT NULL, payload TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_budgets(provider TEXT PRIMARY KEY, daily_call_limit INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS provider_calls(id TEXT PRIMARY KEY, provider TEXT NOT NULL, operation TEXT NOT NULL, requested_at TEXT NOT NULL, local_day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ocr_observations(evidence_id TEXT REFERENCES evidence(id), method TEXT NOT NULL, response_json TEXT NOT NULL, parser_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(evidence_id,method));
CREATE TABLE IF NOT EXISTS lesson_revisions(id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL REFERENCES lessons(id), old_payload TEXT NOT NULL, new_payload TEXT NOT NULL, revised_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media_resolutions(content_id TEXT REFERENCES contents(id), reference TEXT NOT NULL, local_path TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(content_id,reference));
CREATE TABLE IF NOT EXISTS api_observations(id TEXT PRIMARY KEY, publication_id TEXT NOT NULL REFERENCES publications(id), evidence_id TEXT NOT NULL REFERENCES evidence(id), endpoint TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reference_observations(id TEXT PRIMARY KEY, reference_id TEXT REFERENCES reference_posts(id), payload TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lessons(id TEXT PRIMARY KEY, report_id TEXT REFERENCES review_reports(id), text TEXT NOT NULL, conditions TEXT NOT NULL, counterexamples TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_validation(config_version_id TEXT PRIMARY KEY REFERENCES model_config_versions(id), status TEXT NOT NULL, resolved_model TEXT, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_queue(id TEXT PRIMARY KEY, evidence_id TEXT REFERENCES evidence(id), reason TEXT NOT NULL, payload TEXT NOT NULL, resolution TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS credentials(id TEXT PRIMARY KEY, provider TEXT NOT NULL, encrypted BLOB NOT NULL, mask TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_settings(provider TEXT PRIMARY KEY, credential_ref TEXT REFERENCES credentials(id), status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_configs(id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, current_version INTEGER NOT NULL, archived_at TEXT);
CREATE TABLE IF NOT EXISTS model_config_versions(id TEXT PRIMARY KEY, config_id TEXT REFERENCES model_configs(id), version INTEGER NOT NULL, model_id TEXT, base_url TEXT, credential_ref TEXT REFERENCES credentials(id), parameters TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(config_id,version));
CREATE TABLE IF NOT EXISTS active_model(id INTEGER PRIMARY KEY CHECK(id=1), config_version_id TEXT NOT NULL REFERENCES model_config_versions(id), revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS topic_candidates(id TEXT PRIMARY KEY, title TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, profile_version INTEGER NOT NULL, rules_version INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS creation_plans(id TEXT PRIMARY KEY, markdown_text TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_reports(id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope_json TEXT NOT NULL, computed_metrics_json TEXT NOT NULL, report_json TEXT NOT NULL, html_text TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reference_posts(id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT, author TEXT, fans REAL, likes REAL, saves REAL, comments REAL, observed_at TEXT NOT NULL, payload TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS trend_samples(id TEXT PRIMARY KEY, platform TEXT NOT NULL, upstream_key TEXT NOT NULL, observed_at TEXT, collected_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(platform,upstream_key));
CREATE TABLE IF NOT EXISTS hot_topic_snapshots(id TEXT PRIMARY KEY, platform TEXT NOT NULL, collected_at TEXT NOT NULL, profile_version INTEGER NOT NULL, keywords TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS upload_sessions(id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT, count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit_events(id TEXT PRIMARY KEY, action TEXT NOT NULL, object_id TEXT, created_at TEXT NOT NULL);
'''

PROFILE = dict(xiaohongshu_name='一路向阳的AI', douyin_name='一路向阳的AI', position='AI 实战、职场提效、产品实践', audience='希望将 AI 用到真实工作中的职场人', promise='分享经过实践的 AI 工作方法', primary_goal='涨粉', secondary_goal='个人品牌', formats=['image_post','video'], weekly_target=3, style='真实、具体、有过程', avoid='夸大效果、缺少证据的结论')
TOPIC = dict(keywords=['AI','Codex','WorkBuddy','Agent','Skills'], exclusions=[], weights={'用户需求':20,'定位与受众':20,'涨粉潜力':15,'品牌积累':15,'变现潜力':15,'差异化':10,'制作可行性':5}, platforms=['douyin','xiaohongshu','zhihu','bilibili'])
REVIEW = dict(min_observation=3, min_sample=5, compare_same_platform=True, compare_same_format=True, separate_paid=True)

def path(): return ROOT / 'data' / 'workspace.db'

def initialize():
    # A missing previously initialized database is a recovery condition, not a new empty workspace.
    marker = ROOT / 'data' / '.initialized'
    if marker.exists() and not path().exists():
        raise RuntimeError('原数据库缺失，请从备份恢复；没有自动建立空库。')
    if not ROOT.parent.exists(): raise RuntimeError('数据盘不可用。')
    for part in ['data','content','evidence','inbox','exports/plans','exports/reviews','backups','logs']:
        (ROOT / part).mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        if 'inbox_cleared_at' not in {row['name'] for row in c.execute('PRAGMA table_info(evidence)')}:
            c.execute('ALTER TABLE evidence ADD COLUMN inbox_cleared_at TEXT')
            c.execute('INSERT OR IGNORE INTO schema_migrations VALUES(2,?)',(now(),))
        if 'homepage' not in {row['name'] for row in c.execute('PRAGMA table_info(accounts)')}:
            c.execute('ALTER TABLE accounts ADD COLUMN homepage TEXT')
            c.execute('INSERT OR IGNORE INTO schema_migrations VALUES(3,?)',(now(),))
        if 'sec_uid' not in {row['name'] for row in c.execute('PRAGMA table_info(accounts)')}:
            c.execute('ALTER TABLE accounts ADD COLUMN sec_uid TEXT')
            c.execute('INSERT OR IGNORE INTO schema_migrations VALUES(4,?)',(now(),))
        publications_info = c.execute('PRAGMA table_info(publications)').fetchall()
        content_column = next((row for row in publications_info if row['name'] == 'content_id'), None)
        if content_column and content_column['notnull']:
            # SQLite cannot drop a NOT NULL constraint in place: rebuild the table.
            c.commit()
            c.execute('PRAGMA foreign_keys=OFF')
            c.execute('BEGIN IMMEDIATE')
            c.execute('''CREATE TABLE publications_new(id TEXT PRIMARY KEY, content_id TEXT REFERENCES contents(id), account_id TEXT NOT NULL REFERENCES accounts(id), platform TEXT NOT NULL, platform_post_id TEXT, canonical_url TEXT, status TEXT NOT NULL, title_override TEXT, cover_url TEXT, platform_kind TEXT, published_local_date TEXT, published_at TEXT, published_precision TEXT NOT NULL DEFAULT 'unknown', published_time_status TEXT NOT NULL DEFAULT 'unknown', published_evidence_id TEXT REFERENCES evidence(id), version INTEGER NOT NULL DEFAULT 1, UNIQUE(platform,platform_post_id))''')
            c.execute('''INSERT INTO publications_new(id,content_id,account_id,platform,platform_post_id,canonical_url,status,title_override,cover_url,platform_kind,published_local_date,published_at,published_precision,published_time_status,published_evidence_id,version)
                SELECT id,content_id,account_id,platform,platform_post_id,canonical_url,status,title_override,NULL,NULL,published_local_date,published_at,published_precision,published_time_status,published_evidence_id,version FROM publications''')
            c.execute('DROP TABLE publications')
            c.execute('ALTER TABLE publications_new RENAME TO publications')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_publication_url ON publications(canonical_url) WHERE canonical_url IS NOT NULL')
            c.execute('COMMIT')
            c.execute('PRAGMA foreign_keys=ON')
            broken = c.execute('PRAGMA foreign_key_check').fetchall()
            if broken: raise RuntimeError('作品表迁移后外键检查失败，已中止：' + str(broken[:3]))
            c.execute('INSERT OR IGNORE INTO schema_migrations VALUES(5,?)',(now(),))
        c.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)',(now(),))
        if not c.execute('SELECT 1 FROM workspace_settings').fetchone():
            c.execute('INSERT INTO profile_versions VALUES(1,?,?)',(dump(PROFILE),now()))
            for kind, value in [('topic',TOPIC),('review',REVIEW)]:
                c.execute('INSERT INTO rule_versions(kind,version,payload,created_at) VALUES(?,1,?,?)',(kind,dump(value),now()))
            c.execute('INSERT INTO workspace_settings VALUES(1,1,1,1)')
            for platform in ['xiaohongshu','douyin']:
                c.execute('INSERT INTO accounts(id,platform,nickname) VALUES(?,?,?)',(uid(),platform,PROFILE[platform+'_name']))
            c.execute("INSERT INTO model_configs VALUES('codex','Codex SDK · 当前 ChatGPT 账号','codex',1,NULL)")
            c.execute("INSERT INTO model_config_versions VALUES('codex-v1','codex',1,NULL,NULL,NULL,'{}',?)",(now(),))
            c.execute("INSERT INTO active_model VALUES(1,'codex-v1',1)")
        # Never automatically repeat an inference that might still exist upstream.
        c.execute("UPDATE jobs SET state=CASE WHEN type IN ('review','plan','topics','model_test','trend_relevance','screenshot_batch') THEN 'recovery_required' ELSE 'interrupted' END, updated_at=? WHERE state IN ('running','cancelling')",(now(),))
    marker.touch(exist_ok=True)

@contextmanager
def connect():
    c = sqlite3.connect(path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=FULL')
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally: c.close()

def rows(c, sql, args=()): return [dict(x) for x in c.execute(sql,args).fetchall()]
def one(c, sql, args=()):
    row=c.execute(sql,args).fetchone()
    return dict(row) if row else None

def snapshot(c):
    s=one(c,'SELECT * FROM workspace_settings')
    p=load(c.execute('SELECT payload FROM profile_versions WHERE version=?',(s['profile_version'],)).fetchone()[0])
    t=load(c.execute("SELECT payload FROM rule_versions WHERE kind='topic' AND version=?",(s['topic_version'],)).fetchone()[0])
    r=load(c.execute("SELECT payload FROM rule_versions WHERE kind='review' AND version=?",(s['review_version'],)).fetchone()[0])
    return dict(profile=p, rules=t, review_rules=r, profile_version=s['profile_version'], topic_version=s['topic_version'], review_version=s['review_version'])

def audit(c, action, object_id=None): c.execute('INSERT INTO audit_events VALUES(?,?,?,?)',(uid(),action,object_id,now()))

def confined(relative):
    result=(ROOT / relative).resolve()
    if not result.is_relative_to(ROOT.resolve()): raise ValueError('文件路径超出数据目录')
    return result
