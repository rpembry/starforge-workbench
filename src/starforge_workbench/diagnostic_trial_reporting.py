"""Public console boundary for protected local synthetic trial evidence.

Numeric provenance, reports, hashes, paths and verification details remain in
the owner-local evidence file. Console output contains only the registered
diagnostic status schema. This helper authenticates neither evidence nor humans.
"""
import json
import os
import stat
import sys

from .diagnostic_release import ReleaseError, automatic_status

MAX_EVIDENCE_BYTES = 65536


def public_status(evidence):
    """Select a bounded status; malformed evidence produces content-free failure."""
    try:
        if type(evidence) is not dict:
            raise ValueError()
        value = evidence['status']
        if (type(value) is not dict or set(value) != {'protocol', 'status'}
                or type(value['protocol']) is not str
                or value['protocol'] != 'diagnostic.status.v1'
                or type(value['status']) is not str):
            raise ValueError()
        return automatic_status(value['status'])
    except (KeyError, TypeError, ValueError, ReleaseError):
        return automatic_status('failed')


def emit_status(evidence, *, stdout=None):
    """Emit only canonical status fields; never serialize local evidence."""
    output = sys.stdout if stdout is None else stdout
    output.write(json.dumps(public_status(evidence), sort_keys=True) + '\n')


def read_local_evidence(path):
    """Trusted operator path only; protect against links, devices and oversized data."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_EVIDENCE_BYTES):
            raise ValueError()
        data = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(data) > MAX_EVIDENCE_BYTES:
            raise ValueError()
        return json.loads(data)


def main(args=None):
    args = sys.argv[1:] if args is None else args
    try:
        if len(args) != 1:
            raise ValueError()
        evidence = read_local_evidence(args[0])
    except (OSError, ValueError, TypeError, RecursionError):
        evidence = None
    # Deliberately no argparse usage, exception text, or stderr diagnostics:
    # an error may contain a private path or untrusted source content.
    emit_status(evidence)


if __name__ == '__main__':
    main()
