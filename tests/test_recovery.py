import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from backend import db,worker,models
from test_workspace import client

def test_recovery_keeps_lock_until_explicit_termination(client):
    job=worker.enqueue('model_test',{})
    with db.connect() as c: c.execute("UPDATE jobs SET state='recovery_required' WHERE id=?",(job['id'],))
    assert client.post('/api/jobs/'+job['id']+'/retry').status_code==409
    assert client.post('/api/jobs/'+job['id']+'/resolve',json={'reason':'尚未核实'}).status_code==400
    assert client.post('/api/jobs/'+job['id']+'/resolve',json={'remote_terminated':True,'reason':'已检查远端任务终止'}).status_code==200
    with db.connect() as c:
        assert not models.busy(c)
        assert db.one(c,'SELECT state FROM jobs WHERE id=?',(job['id'],))['state']=='failed'

def test_offline_restore_preserves_previous_database(client):
    worker.worker.stop()
    backup=db.ROOT/'backups'/'test.db'
    with db.connect() as c,sqlite3.connect(backup) as dest: c.backup(dest)
    with db.connect() as c: c.execute("UPDATE accounts SET nickname='after-backup'")
    env={**os.environ,'XIANGYANG_DATA_ROOT':str(db.ROOT)}
    result=subprocess.run([sys.executable,'scripts/restore-database.py',str(backup)],env=env,capture_output=True)
    assert result.returncode==0,result.stderr
    with db.connect() as c: assert db.one(c,'SELECT nickname FROM accounts')['nickname']!='after-backup'
    previous=list((db.ROOT/'backups').glob('before-restore-*.db'))
    assert len(previous)==1
    with sqlite3.connect(previous[0]) as c: assert c.execute('SELECT nickname FROM accounts').fetchone()[0]=='after-backup'

def test_review_rules_frozen_with_task(client):
    before=client.get('/api/profile').json()
    assert before['review_rules']['min_sample']==5
    response=client.put('/api/rules/review',json={'expected_version':1,'payload':{**before['review_rules'],'min_sample':9}})
    assert response.status_code==200
    job=worker.enqueue('model_test',{})
    with db.connect() as c:
        payload=db.load(db.one(c,'SELECT payload FROM jobs WHERE id=?',(job['id'],))['payload'])
        assert payload['snapshot']['review_rules']['min_sample']==9
        c.execute("UPDATE jobs SET state='failed' WHERE id=?",(job['id'],))
