"""Shared hashing helpers for dedupe keys and file integrity checks."""
import hashlib
from . import db

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def digest(value): return hashlib.sha256(db.dump(value).encode()).hexdigest()
