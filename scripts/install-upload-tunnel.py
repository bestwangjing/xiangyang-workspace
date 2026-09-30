"""Install the official Windows upload tunnel binary after SHA256 verification."""
import hashlib
from pathlib import Path
import httpx
root=Path(__file__).resolve().parent.parent/'tools'
root.mkdir(exist_ok=True)
with httpx.Client(timeout=60,follow_redirects=True) as client:
    response=client.get('https://api.github.com/repos/cloudflare/cloudflared/releases/latest');response.raise_for_status()
    asset=next(x for x in response.json()['assets'] if x['name']=='cloudflared-windows-amd64.exe')
    digest=asset.get('digest','')
    if not digest.startswith('sha256:'):raise RuntimeError('Official release has no SHA256; installation stopped')
    url=asset['browser_download_url']
    if not url.startswith('https://github.com/cloudflare/cloudflared/releases/download/'):raise RuntimeError('Unexpected download source')
    target=root/'cloudflared.download';checksum=hashlib.sha256()
    with client.stream('GET',url) as download:
        download.raise_for_status()
        with target.open('wb') as file:
            for chunk in download.iter_bytes():file.write(chunk);checksum.update(chunk)
    if checksum.hexdigest()!=digest.split(':')[1]:raise RuntimeError('SHA256 mismatch; installation stopped')
    target.replace(root/'cloudflared.exe')
print('Official cloudflared installed with verified SHA256.')
