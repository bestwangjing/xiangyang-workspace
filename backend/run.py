"""One process per data directory. The management server never listens on LAN."""
import os
import sys
from . import db

def main():
    db.ROOT.joinpath('data').mkdir(parents=True,exist_ok=True)
    lock=open(db.ROOT/'data'/'service.lock','a+b')
    lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        print('工作台已在运行，请打开 http://127.0.0.1:8766');sys.exit(1)
    import uvicorn
    uvicorn.run('backend.main:app',host='127.0.0.1',port=int(os.environ.get('XIANGYANG_PORT','8766')),access_log=False)

if __name__=='__main__': main()
