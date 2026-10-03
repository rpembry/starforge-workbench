"""Host-side bounded Unix worker channel, with host-only durable inbox state."""

import errno
import json
import os
from pathlib import Path
import socket
import socketserver
import threading

from .worker_protocol import MAX_FRAME, ProtocolError, WorkerInbox, decode_frame


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        while True:
            try:
                line = self.rfile.readline(MAX_FRAME + 1)
                if not line:
                    return
                if len(line) > MAX_FRAME:
                    raise ProtocolError("oversized worker frame")
                frame = decode_frame(line)
                with self.server.inbox_lock:
                    response = self.server.inbox.accept(frame)
            except (ProtocolError, ValueError, OSError):
                response = {"status": "rejected"}
                self.wfile.write((json.dumps(response) + "\n").encode())
                return
            self.wfile.write((json.dumps(response) + "\n").encode())


class WorkerChannel:
    def __init__(self, attempt_root, *, job_id, attempt_id, incarnation):
        attempt = Path(attempt_root).absolute()
        if (not attempt.is_dir() or attempt.is_symlink() or
                attempt.stat().st_uid != os.getuid() or attempt.stat().st_mode & 0o077):
            raise ValueError("attempt root must be private")
        self.mount_dir = attempt / "channel"
        self.inbox_dir = attempt / "host-inbox"
        self.mount_dir.mkdir(mode=0o700, exist_ok=True)
        self.inbox_dir.mkdir(mode=0o700, exist_ok=True)
        for path in (self.mount_dir, self.inbox_dir):
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError("worker channel directory changed")
        self.inbox = WorkerInbox(self.inbox_dir, job_id=job_id,
                                 attempt_id=attempt_id, incarnation=incarnation)
        self.socket_path = self.mount_dir / "events.sock"
        # Linux AF_UNIX sun_path is short (usually 108 bytes). Bind through a
        # directory fd so deeply nested private state roots still work. The
        # worker sees the ordinary short /channel/events.sock mount path.
        self.directory_fd = os.open(self.mount_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.connect_path = f"/proc/self/fd/{self.directory_fd}/events.sock"
        try:
            if self.socket_path.exists() or self.socket_path.is_symlink():
                if not self.socket_path.is_socket():
                    raise ValueError("worker channel path changed")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    try:
                        probe.settimeout(0.2)
                        probe.connect(self.connect_path)
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            raise
                    else:
                        raise ValueError("worker channel already served")
                self.socket_path.unlink()  # stale socket in exact private attempt
            self.server = _Server(self.connect_path, _Handler)
        except BaseException:
            os.close(self.directory_fd)
            raise
        self.server.inbox = self.inbox
        self.server.inbox_lock = threading.Lock()
        self.socket_path.chmod(0o600)
        self.inode = self.socket_path.stat().st_ino
        self.thread = threading.Thread(target=self.server.serve_forever, name="worker-" + attempt_id, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.socket_path.is_socket() and self.socket_path.stat().st_ino == self.inode:
            self.socket_path.unlink()
        os.close(self.directory_fd)
