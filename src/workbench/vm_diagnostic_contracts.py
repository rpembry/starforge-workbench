"""Fixed local file/log contracts. No host reads, journal execution or MCP tool."""
from dataclasses import dataclass
import json
import re

from .vm_diagnostics import Diagnostic, _decode


class ContractError(RuntimeError):
    def __init__(self):
        super().__init__('Local diagnostic contract unavailable')


def numeric_file(operation, raw):
    """Publish only the existing exact typed schema, never an arbitrary file body."""
    try:
        if type(operation) is not Diagnostic:
            raise ValueError
        return {'schema_version': 1, 'status': 'ok', 'diagnostic': operation.value,
                'data': _decode(operation, raw)}
    except Exception:
        raise ContractError() from None


@dataclass(frozen=True, slots=True)
class LogWindow:
    service: str
    since: int
    until: int
    max_lines: int = 100
    max_bytes: int = 8192

    def __post_init__(self):
        if (type(self.service) is not str or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}\.service', self.service)
                or type(self.since) is not int or type(self.until) is not int
                or not 0 <= self.since < self.until <= 2**40 or self.until - self.since > 300
                or type(self.max_lines) is not int or not 1 <= self.max_lines <= 100
                or type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 8192):
            raise ContractError()

    def journal_argv(self):
        # For a future supervised local producer only. Caller must bound wall
        # time and byte collection independently; --lines does not cap bytes.
        return ('/usr/bin/journalctl', '--no-pager', '--quiet', '--output=json',
                '--unit='+self.service, '--since=@'+str(self.since), '--until=@'+str(self.until),
                '--lines='+str(self.max_lines))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def log_counts(window, raw):
    """Consume a local normalized fixture; return only a fixed count schema.

    Producer must normalize journal fields without expanding collection scope.
    MESSAGE is validated/discarded here, never returned in failures or results.
    This is not a general syslog parser or an outbound release authorization.
    """
    try:
        if type(window) is not LogWindow or type(raw) is not bytes or len(raw) > window.max_bytes:
            raise ValueError
        lines = raw.splitlines()
        if len(lines) > window.max_lines:
            raise ValueError
        counts = [0] * 8
        for line in lines:
            row = json.loads(line.decode('utf-8'), object_pairs_hook=_unique)
            if (type(row) is not dict or set(row) != {'service', 'timestamp', 'priority', 'message'}
                    or type(row['service']) is not str or row['service'] != window.service
                    or type(row['timestamp']) is not int or not window.since <= row['timestamp'] <= window.until
                    or type(row['priority']) is not int or not 0 <= row['priority'] <= 7
                    or type(row['message']) is not str or len(row['message'].encode('utf-8')) > 1024):
                raise ValueError
            counts[row['priority']] += 1
        return {'schema_version': 1, 'diagnostic': 'local_log_counts', 'status': 'ok',
                'data': {'record_count': len(lines), 'priority_counts': counts}}
    except Exception:
        raise ContractError() from None
