"""Prepare identical Alpine BusyBox/musl bytes without installing a host toolchain."""
import hashlib
import json
from pathlib import Path
import shlex
import tarfile
import time

from . import docker_worker as w
from .execution import parse_profile

MEMBERS = ('bin/busybox', 'lib/ld-musl-x86_64.so.1')
APPLETS = ('sh', 'mkdir', 'cat', 'sed', 'ln', 'sleep', 'awk', 'ls', 'touch', 'sha256sum')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_toolchain(store, destination):
    manifest = json.loads((store/'manifest.json').read_text())
    destination.mkdir(mode=0o700)
    found = {}
    for layer in manifest['layers']:
        algorithm, digest = layer['digest'].split(':', 1)
        if algorithm != 'sha256' or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise w.WorkerError('Unsupported image layer digest')
        blob = store/digest
        if sha(blob) != digest: raise w.WorkerError('Image layer checksum mismatch')
        with tarfile.open(blob, 'r:*') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                if name not in MEMBERS: continue
                if not member.isfile() or member.size > 16*1024*1024:
                    raise w.WorkerError('Unsupported toolchain archive member')
                data = archive.extractfile(member).read()
                target = destination/Path(name).name
                target.write_bytes(data); target.chmod(0o500)
                found[name] = sha(target)
    if set(found) != set(MEMBERS):
        raise w.WorkerError('Image does not contain the supported x86_64 BusyBox/musl toolchain')
    wrappers = destination/'bin'; wrappers.mkdir(mode=0o700)
    for applet in APPLETS:
        script = wrappers/applet
        argv = [str(destination/'ld-musl-x86_64.so.1'), str(destination/'busybox'), applet]
        script.write_text('#!/bin/sh\nexec '+shlex.join(argv)+' "$@"\n')
        script.chmod(0o500)
    return found


def prepare(root, image):
    # Reuse the strict digest validator before using an external registry argument.
    from .worker_evaluation import profile
    parse_profile(profile(image))
    store = root/'cold-image'
    if store.exists() or (root/'toolchain').exists():
        raise w.WorkerError('Cold preparation requires new task-specific storage')
    began = time.monotonic()
    w.command(['skopeo','--override-os','linux','--override-arch','amd64','copy',
               'docker://'+image,'dir:'+str(store)], timeout=300)
    downloaded = time.monotonic()
    hashes = extract_toolchain(store, root/'toolchain')
    native_ready = time.monotonic()
    result = dict(image=image, download_s=downloaded-began,
                  native_extraction_s=native_ready-downloaded,
                  total_preparation_s=native_ready-began,
                  stored_bytes=sum(p.stat().st_size for p in store.iterdir() if p.is_file()),
                  extracted_toolchain_bytes=sum(p.stat().st_size for p in (root/'toolchain').rglob('*') if p.is_file()),
                  native_toolchain_hashes=hashes,
                  conditions='Fresh registry download directory; shared Docker and OS caches retained',
                  native_setup='Extract identical BusyBox and musl; applet wrappers use private absolute paths',
                  scope='Linux x86_64 Alpine BusyBox/musl only')
    w.atomic(root/'preparation.json',result)
    return result
