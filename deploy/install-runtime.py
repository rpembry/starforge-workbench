#!/usr/bin/env python3
"""Root-only install of pinned vendor runtime binaries; never changes existing apps."""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import urllib.request

if os.geteuid() != 0:
    raise SystemExit('Run as root after reviewing deploy/runtime-downloads.json')
root = Path('/opt/workbench-runtime')
root.mkdir(mode=0o755, exist_ok=True)
for item in json.loads(Path(__file__).with_name('runtime-downloads.json').read_text()):
    with urllib.request.urlopen(item['url'], timeout=60) as response:
        blob = response.read()
    if hashlib.sha256(blob).hexdigest() != item['sha256']:
        raise SystemExit('Vendor checksum mismatch: '+item['name'])
    if item['name'].startswith('uv-'):
        with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as archive:
            member = archive.getmember('uv-x86_64-unknown-linux-gnu/uv')
            if not member.isfile():
                raise SystemExit('Unexpected uv archive member')
            binary = archive.extractfile(member).read()
        destination = root/'uv'
    else:
        binary = blob
        destination = Path('/usr/local/bin/cloudflared')
    if destination.exists() and destination.read_bytes() != binary:
        raise SystemExit('Existing different binary needs separate review: '+str(destination))
    destination.write_bytes(binary)
    destination.chmod(0o755)
    print('Verified and installed', item['name'], item['version'])
