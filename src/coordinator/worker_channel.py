"""Host-side worker transports with host-only durable inbox state."""

import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import tempfile
import threading
import hashlib

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


class MailboxChannel:
    """One-slot file transport; the durable inbox, not mailbox files, is authority.

    Regular files cross the SELinux container/host boundary without granting a
    container permission to connect to the host process's Unix socket domain.
    A worker may alter mailbox bytes or fabricate an acknowledgement, but only
    host-validated frames in the separate inbox can establish result evidence.
    """

    REQUEST = "request.json"
    ACK = "ack.json"
    POLL_SECONDS = 0.05
    MAX_ACK = 512

    @staticmethod
    def _lock_inbox(inbox_dir):
        """Keep a host-only inode locked for this channel's entire lifetime.

        The lock file is never unlinked by channel cleanup. Host processes that
        can rewrite this private directory must coordinate outside this lock.
        """
        directory_fd = os.open(inbox_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open("channel.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                current = os.stat("channel.lock", dir_fd=directory_fd, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                        info.st_uid != os.getuid() or info.st_mode & 0o077 or
                        (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                    raise ValueError("mailbox lock path changed")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("mailbox channel already served") from None
                current = os.stat("channel.lock", dir_fd=directory_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError("mailbox lock path changed")
                return fd
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(directory_fd)

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
            if (path.is_symlink() or path.stat().st_uid != os.getuid() or
                    path.stat().st_mode & 0o077):
                raise ValueError("mailbox directory changed")
        self.lock_fd = self._lock_inbox(self.inbox_dir)
        try:
            self.inbox = WorkerInbox(self.inbox_dir, job_id=job_id,
                                     attempt_id=attempt_id, incarnation=incarnation)
            self.directory_fd = os.open(self.mount_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self.stop_event = threading.Event()
                self.thread = threading.Thread(target=self._serve, name="mailbox-" + attempt_id, daemon=True)
                self.thread.start()
                self.closed = False
            except BaseException:
                os.close(self.directory_fd)
                raise
        except BaseException:
            os.close(self.lock_fd)
            raise

    def _request(self):
        try:
            fd = os.open(self.REQUEST, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory_fd)
        except FileNotFoundError:
            return None
        except OSError:
            return None  # hostile symlink or inaccessible file cannot enter the inbox
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.getuid() or info.st_mode & 0o077 or
                    info.st_size > MAX_FRAME):
                return None
            wire = os.read(fd, MAX_FRAME + 1)
            if len(wire) != info.st_size or len(wire) > MAX_FRAME:
                return None
            return info.st_ino, wire
        finally:
            os.close(fd)

    def _remove_request(self, inode):
        try:
            current = os.stat(self.REQUEST, dir_fd=self.directory_fd, follow_symlinks=False)
            if current.st_ino == inode and stat.S_ISREG(current.st_mode):
                os.unlink(self.REQUEST, dir_fd=self.directory_fd)
                os.fsync(self.directory_fd)
        except FileNotFoundError:
            pass

    def _ack(self, frame, wire, response):
        body = {"seq": frame.seq, "event_id": frame.event_id,
                "frame_sha256": hashlib.sha256(wire).hexdigest(), "response": response}
        data = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(data) > self.MAX_ACK:
            raise ProtocolError("mailbox acknowledgement too large")
        fd, path = tempfile.mkstemp(prefix=".ack-", dir=self.mount_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(Path(path).name, self.ACK,
                       src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _serve(self):
        # Fixed names plus a single pending sender frame bound legitimate queue
        # depth. Invalid or replaced files never become host evidence.
        while not self.stop_event.wait(self.POLL_SECONDS):
            request = self._request()
            if request is None:
                continue
            inode, wire = request
            try:
                frame = decode_frame(wire)
                try:
                    response = self.inbox.accept(frame)  # fsync before ack
                except ProtocolError:
                    response = {"status": "rejected"}
                self._remove_request(inode)
                self._ack(frame, wire, response)
            except (ProtocolError, OSError, ValueError):
                # A malformed or raced worker file is not authoritative. The
                # sender retains its pending frame and eventually times out.
                continue

    def close(self):
        if self.closed:
            return
        self.stop_event.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError("mailbox channel did not stop")
        os.close(self.directory_fd)
        os.close(self.lock_fd)
        self.closed = True
