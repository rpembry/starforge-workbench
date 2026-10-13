import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest
from starforge_workbench import evaluation_toolchain as t
from starforge_workbench import docker_worker as w


def image_store(root, symlink=False):
    store=root/'store';store.mkdir()
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode='w:gz') as archive:
        for name in t.MEMBERS:
            member=tarfile.TarInfo(name)
            if symlink:
                member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd'
                archive.addfile(member)
            else:
                member.size=3;archive.addfile(member,io.BytesIO(b'elf'))
    blob=data.getvalue();checksum=hashlib.sha256(blob).hexdigest()
    (store/checksum).write_bytes(blob)
    (store/'manifest.json').write_text(json.dumps({'layers':[{'digest':'sha256:'+checksum}]}))
    return store,checksum


def test_extracts_only_verified_members_into_private_toolchain(tmp_path):
    store,_=image_store(tmp_path)
    output=tmp_path/'toolchain'
    hashes=t.extract_toolchain(store,output)
    assert set(hashes)==set(t.MEMBERS)
    assert (output/'busybox').read_bytes()==b'elf'
    assert (output/'bin/sh').stat().st_mode & 0o777 == 0o500
    assert 'exec ' in (output/'bin/sh').read_text()


def test_rejects_changed_layer_before_extraction(tmp_path):
    store,checksum=image_store(tmp_path)
    (store/checksum).write_bytes(b'corrupt')
    with pytest.raises(w.WorkerError,match='checksum'):
        t.extract_toolchain(store,tmp_path/'toolchain')


def test_rejects_toolchain_symlink(tmp_path):
    store,_=image_store(tmp_path,True)
    with pytest.raises(w.WorkerError,match='archive member'):
        t.extract_toolchain(store,tmp_path/'toolchain')
