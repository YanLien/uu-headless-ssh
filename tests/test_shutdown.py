"""Verify graceful client cleanup without connecting to UU or another host."""
import os
import pty
import signal
import time
import unittest

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
        os.close(master)
        os.close(reader)

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


if __name__ == "__main__":
    unittest.main()
