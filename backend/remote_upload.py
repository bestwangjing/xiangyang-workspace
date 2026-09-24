"""An on-demand, expiring tunnel to the upload-only listener (never management)."""
import atexit
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from fastapi import HTTPException

BINARY=Path(__file__).resolve().parent.parent/'tools'/'cloudflared.exe'


def child_job(process):
    """Windows closes this job handle if the app crashes, terminating the tunnel."""
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes as w
    class Limits(ctypes.Structure):
        _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(ctypes.Structure):
        _fields_=[(name,ctypes.c_ulonglong) for name in ['read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes']]
    class Extended(ctypes.Structure):
        _fields_=[('limits',Limits),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateJobObjectW(None,None);info=Extended();info.limits.flags=0x2000
    if not handle or not kernel.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle))):
        if handle:kernel.CloseHandle(handle)
        process.terminate();process.wait(timeout=5)
        raise HTTPException(503,'无法建立远程通道的自动关闭保护，未开放上传')
    return lambda:kernel.CloseHandle(handle)


class UploadTunnel:
    def __init__(self):
        self.lock=threading.RLock()
        self.process=None
        self.url=None
        self.leases={}
        self.close_job=None

    def _stop(self):
        process=self.process
        self.process=None;self.url=None;self.leases.clear()
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        if self.close_job:self.close_job();self.close_job=None

    def stop(self):
        with self.lock:self._stop()

    def revoke(self,session_id):
        with self.lock:
            self.leases.pop(session_id,None)
            if not self.leases:self._stop()

    def prune(self):
        with self.lock:
            self.leases={key:until for key,until in self.leases.items() if until>time.monotonic()}
            if not self.leases:self._stop()

    def start(self,session_id,ttl=600):
        with self.lock:
            if self.process and self.process.poll() is None and self.url:
                self.leases[session_id]=time.monotonic()+ttl
                return self.url
            self._stop()
            if not BINARY.is_file():raise HTTPException(503,'远程上传组件未安装，请运行 scripts/install-upload-tunnel.py')
            # A private empty config avoids using/modifying any user tunnel configuration.
            config=BINARY.parent/'upload-tunnel.yml'
            config.write_text('{}\n',encoding='utf-8')
            ready=threading.Event();found={}
            self.process=subprocess.Popen([str(BINARY),'tunnel','--config',str(config),'--no-autoupdate','--url','http://127.0.0.1:'+str(int(os.environ.get('XIANGYANG_UPLOAD_PORT','8767'))),'--protocol','http2'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            process=self.process
            self.close_job=child_job(process)
            def consume():
                for line in process.stdout:
                    match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
                    if match:found['url']=match.group()
                    if 'Registered tunnel connection' in line:found['connected']=True
                    if found.get('url') and found.get('connected'):ready.set()
                process.stdout.close()
            threading.Thread(target=consume,daemon=True,name='upload-tunnel-output').start()
            if not ready.wait(35):
                self._stop()
                raise HTTPException(503,'临时远程通道连接失败，当前网络可能无法连接 Cloudflare；请稍后再试或使用同一 Wi-Fi 上传。')
            self.url=found['url'];self.leases[session_id]=time.monotonic()+ttl
            def expire():
                while process.poll() is None:
                    time.sleep(2)
                    with self.lock:
                        if self.process is not process:return
                        self.prune()
            threading.Thread(target=expire,daemon=True,name='upload-tunnel-expiry').start()
            return self.url


tunnel=UploadTunnel()
atexit.register(tunnel.stop)
