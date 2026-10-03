"""Conservative reader for the optional, read-only GNOME favorite presence service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


HELPER = Path(__file__).with_name('gnome_presence_helper.py')


class GnomeDesktop:
    def __init__(self, *, runner=subprocess.run, proc=Path('/proc')):
        self.runner = runner
        self.proc = Path(proc)

    def observe(self, favorite: dict, entry: dict) -> tuple[str, str]:
        if os.environ.get('XDG_SESSION_TYPE') != 'wayland':
            return 'unavailable', 'Not a Wayland session'
        try:
            completed = self.runner(['/usr/bin/python3', str(HELPER),
                                     favorite['desktop_id']], capture_output=True,
                                    text=True, timeout=4, check=True)
            result = json.loads(completed.stdout)
            if result.get('state') == 'present':
                return 'present', 'GNOME reports a matching open window'
            if result.get('state') != 'absent':
                if favorite['kind'] == 'pwa':
                    from .favorite_apps import _pwa_cmdline_identity
                    for process in self.proc.iterdir():
                        if not process.name.isdigit():
                            continue
                        try:
                            if process.stat().st_uid != os.getuid():
                                continue
                            executable = process.joinpath('exe').resolve(strict=True)
                            if executable.parent == Path(entry['executable']).parent and _pwa_cmdline_identity(
                                    process.joinpath('cmdline').read_bytes(), favorite) is True:
                                return 'present', 'Exact browser profile and PWA process are running'
                        except FileNotFoundError:
                            continue
                        except OSError:
                            return 'uncertain', 'Browser process inventory is incomplete'
                return 'uncertain', 'GNOME cannot verify this app is closed'
            # Shell's window inventory cannot exclude a starting process.
            # A script launcher may exit while its child stays open under a
            # different executable, so absence cannot be established from it.
            with Path(entry['executable']).open('rb') as stream:
                if stream.read(4) != b'\x7fELF':
                    return 'uncertain', 'Launcher is a wrapper; child process identity is unknown'
            for process in self.proc.iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    if process.stat().st_uid == os.getuid() and process.joinpath('exe').resolve(strict=True) == Path(entry['executable']):
                        return 'uncertain', 'Matching process may still be starting'
                except FileNotFoundError:
                    continue
                except OSError:
                    return 'uncertain', 'Process inventory is incomplete'
            return 'absent', 'GNOME window and user process inventories show no match'
        except (OSError, ValueError, subprocess.SubprocessError, KeyError):
            return 'uncertain', 'GNOME presence service is unavailable or unverified'

    def launch(self, desktop_id: str) -> bool:
        try:
            return subprocess.run(['gtk-launch', desktop_id], timeout=8, check=False,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
