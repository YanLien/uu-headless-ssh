"""Verify graceful client cleanup without connecting to UU or another host."""
from contextlib import contextmanager, redirect_stderr
import errno
import io
import json
import os
from pathlib import Path
import pty
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

import connect
from connect import stop_client


class ClientShutdownTests(unittest.TestCase):
    def start_client(self, ignore_termination=False):
        """A child reports readiness, then simulates cleanup after SIGTERM."""
        reader, writer = os.pipe()
        pid, master = pty.fork()
        if pid == 0:
            os.close(reader)

            def terminate(_signal, _frame):
                time.sleep(0.03)
                os.write(writer, b"cleaned\n")
                os._exit(0)

            handler = signal.SIG_IGN if ignore_termination else terminate
            signal.signal(signal.SIGTERM, handler)
            os.write(writer, b"ready\n")
            while True:
                signal.pause()
        os.close(writer)
        self.assertEqual(os.read(reader, 6), b"ready\n")
        return pid, master, reader

    def close_client(self, pid, master, reader):
        """Reap only our still-owned child if the assertion or stop call failed."""
        try:
            finished, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            finished = pid
        if not finished:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        try:
            os.close(master)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        os.close(reader)

    @contextmanager
    def wrapper(self, pid, master):
        """Replace only launch/config: main operates on our real local PTY child."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text(json.dumps({
                "uu_device_id": "a" * 16, "ssh_host": "example.invalid",
                "ssh_user": "test", "ssh_port": 22,
            }))
            with patch.object(connect, "ROOT", root), \
                    patch("connect.pty.fork", return_value=(pid, master)), \
                    patch("connect.sys.argv", ["connect.py"]), \
                    patch("connect.os.isatty", return_value=False):
                yield

    def test_pty_stays_alive_until_remote_cleanup_completes(self):
        pid, master, reader = self.start_client()
        try:
            self.assertEqual(stop_client(pid, master, grace=2), 0)
            self.assertEqual(os.read(reader, 8), b"cleaned\n")
        finally:
            self.close_client(pid, master, reader)

    def test_unresponsive_client_is_reaped_after_grace_period(self):
        pid, master, reader = self.start_client(ignore_termination=True)
        try:
            self.assertEqual(stop_client(pid, master, grace=0.05), -signal.SIGKILL)
            self.assertEqual(os.read(reader, 1), b"")
        finally:
            self.close_client(pid, master, reader)

    def test_terminal_setup_failure_still_waits_for_client_cleanup(self):
        """Failure before the input loop must stop and reap the spawned client."""
        pid, master, reader = self.start_client()
        try:
            with self.wrapper(pid, master), \
                    patch("connect.sync_terminal_size", side_effect=OSError("synthetic ioctl failure")):
                with self.assertRaises(OSError):
                    connect.main()
            self.assertEqual(os.read(reader, 8), b"cleaned\n")
        finally:
            self.close_client(pid, master, reader)

    def test_connection_timeout_is_failure_even_if_cleanup_exits_successfully(self):
        """Advance only the deadline; the real child still finishes cleanup."""
        pid, master, reader = self.start_client()
        errors = io.StringIO()
        try:
            clock = iter([0, 36])
            monotonic = time.monotonic
            started = monotonic()

            def advance_deadline():
                return next(clock, 36 + monotonic() - started)

            with self.wrapper(pid, master), redirect_stderr(errors), \
                    patch("connect.time.monotonic", side_effect=advance_deadline):
                self.assertEqual(connect.main(), 1)
            self.assertIn("Timed out", errors.getvalue())
            self.assertEqual(os.read(reader, 8), b"cleaned\n")
        finally:
            self.close_client(pid, master, reader)


if __name__ == "__main__":
    unittest.main()
