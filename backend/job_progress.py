"""Durable progress snapshots for long-running generation jobs."""
from __future__ import annotations

from datetime import datetime, timezone
import statistics

from . import db


DEFAULT_SECONDS = {
    'plan': 420,
    'review': 300,
    'experience': 240,
    'topics': 180,
    'trend_relevance': 120,
    'model_test': 45,
    'screenshot_batch': 180,
}


def _seconds(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _estimate(connection, kind):
    rows = db.rows(connection, '''SELECT p.started_at,p.heartbeat_at
        FROM job_progress p JOIN jobs j ON j.id=p.job_id
        WHERE j.type=? AND j.state='completed' AND p.percent=100
        ORDER BY p.heartbeat_at DESC LIMIT 12''', (kind,))
    samples=[]
    for row in rows:
        start,end=_seconds(row['started_at']),_seconds(row['heartbeat_at'])
        if start is not None and end is not None and 5 <= end-start <= 3600:
            samples.append(end-start)
    if samples:
        return int(max(30,min(1200,statistics.median(samples))))
    return DEFAULT_SECONDS.get(kind,180)


def start(job_id,kind,message='正在准备任务'):
    now=db.now()
    with db.connect() as c:
        estimate=_estimate(c,kind)
        c.execute('''INSERT INTO job_progress(job_id,phase,percent,message,started_at,heartbeat_at,estimated_total_seconds)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET phase=excluded.phase,percent=excluded.percent,
            message=excluded.message,started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,
            estimated_total_seconds=excluded.estimated_total_seconds''',
            (job_id,'preparing',3,message,now,now,estimate))


def update(job_id,phase,percent,message):
    now=db.now();percent=max(0,min(99,int(percent)))
    with db.connect() as c:
        row=db.one(c,'SELECT job_id FROM job_progress WHERE job_id=?',(job_id,))
        if row:
            c.execute('UPDATE job_progress SET phase=?,percent=MAX(percent,?),message=?,heartbeat_at=? WHERE job_id=?',
                (phase,percent,str(message)[:300],now,job_id))
        else:
            job=db.one(c,'SELECT type FROM jobs WHERE id=?',(job_id,))
            if not job:return
            kind=job['type']
            c.execute('INSERT INTO job_progress VALUES(?,?,?,?,?,?,?)',
                (job_id,phase,percent,str(message)[:300],now,now,_estimate(c,kind)))


def finish(job_id,message='任务已完成'):
    now=db.now()
    with db.connect() as c:
        c.execute("UPDATE job_progress SET phase='completed',percent=100,message=?,heartbeat_at=? WHERE job_id=?",
            (message,now,job_id))


def stop(job_id,state,message):
    now=db.now();phase='waiting_quota' if state=='waiting_quota' else 'error'
    with db.connect() as c:
        c.execute('UPDATE job_progress SET phase=?,message=?,heartbeat_at=? WHERE job_id=?',
            (phase,str(message)[:300],now,job_id))


def view(connection,job):
    row=db.one(connection,'SELECT * FROM job_progress WHERE job_id=?',(job['id'],))
    if not row:
        if job['state']=='queued':
            return dict(phase='queued',percent=0,message='任务正在排队',elapsed_seconds=0,
                remaining_seconds=None,estimated_total_seconds=DEFAULT_SECONDS.get(job['type'],180))
        return None
    now=datetime.now(timezone.utc).timestamp();started=_seconds(row['started_at']) or now
    elapsed=max(0,int(now-started));estimate=int(row['estimated_total_seconds'] or DEFAULT_SECONDS.get(job['type'],180))
    remaining=max(0,estimate-elapsed) if job['state'] in ['queued','running'] and elapsed<=estimate else None
    return dict(phase=row['phase'],percent=row['percent'],message=row['message'],started_at=row['started_at'],
        heartbeat_at=row['heartbeat_at'],elapsed_seconds=elapsed,remaining_seconds=remaining,
        estimated_total_seconds=estimate,overdue=job['state']=='running' and elapsed>estimate)
