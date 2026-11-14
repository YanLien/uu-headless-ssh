"""Check local broker startup and privacy without UU, Swift, or remote shells."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fcntl
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import terminal


# This child speaks only Ping/Pong over a real Unix socket. It deliberately
# delays binding to expose competing launchers and retains the inherited lock.
FAKE_SERVER = r'''
import fcntl, json, os, socket, struct, sys, time
descriptor = int(os.environ["UU_TERMINAL_LOCK_FD"])
fcntl.fcntl(descriptor, fcntl.F_GETFD)
path = sys.argv[1]
time.sleep(0.15)
try:
    os.unlink(path)
except FileNotFoundError:
    pass
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
    listener.bind(path)
    listener.listen(8)
    while True:
        connection, _ = listener.accept()
        with connection:
            length = struct.unpack("!I", connection.recv(4))[0]
            payload = bytearray()
            while len(payload) < length:
                payload.extend(connection.recv(length - len(payload)))
            assert json.loads(payload) == "Ping"
            reply = b'{"Pong":1}'
            connection.sendall(struct.pack("!I", len(reply)) + reply)
'''


class BrokerTests(unittest.TestCase):
    def test_competing_launchers_share_one_process_and_recover_after_exit(self):
        """The startup lock survives launcher exit, then releases on child exit."""
        with tempfile.TemporaryDirectory(prefix="uu-broker-test-", dir="/private/tmp") as temporary:
            path = Path(temporary) / "terminal.sock"
            lock = Path(temporary) / "terminal.lock"
            children = []
            spawning = threading.Lock()
            popen = subprocess.Popen

            def local_server(_arguments, **options):
                child = popen([sys.executable, "-c", FAKE_SERVER, str(path)], **options)
                with spawning:
                    children.append(child)
                return child

            try:
                with patch.object(terminal, "runtime_paths", return_value=(path, lock)), \
                        patch.object(terminal, "official_connection_active", return_value=False), \
                        patch.object(terminal.subprocess, "Popen", side_effect=local_server):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(lambda _: terminal.ensure_server("synthetic", Path("unused"), timeout=5), range(2)))
                    self.assertEqual(results, [path, path])
                    self.assertEqual(len(children), 1)

                    # All launcher descriptors are closed, but the child must
                    # still exclude a second broker during this socket's life.
                    with lock.open("r+b") as descriptor:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

                    children[0].terminate()
                    children[0].wait(timeout=3)
                    self.assertTrue(path.exists(), "simulate a crash leaving a stale socket")
                    self.assertEqual(terminal.ensure_server("synthetic", Path("unused"), timeout=5), path)
                    self.assertEqual(len(children), 2)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=3)

    def test_startup_error_is_reported_and_releases_the_lock(self):
        with tempfile.TemporaryDirectory(prefix="uu-broker-test-", dir="/private/tmp") as temporary:
            path = Path(temporary) / "terminal.sock"
            lock = Path(temporary) / "terminal.lock"
            popen = subprocess.Popen

            def failed_server(_arguments, **options):
                return popen([sys.executable, "-c", "import sys; print('synthetic startup failure', file=sys.stderr); sys.exit(3)"], **options)

            with patch.object(terminal, "runtime_paths", return_value=(path, lock)), \
                    patch.object(terminal, "official_connection_active", return_value=False), \
                    patch.object(terminal.subprocess, "Popen", side_effect=failed_server):
                with self.assertRaisesRegex(RuntimeError, "synthetic startup failure"):
                    terminal.ensure_server("synthetic", Path("unused"), timeout=3)
            with lock.open("r+b") as descriptor:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_official_connection_is_not_displaced_by_starting_a_broker(self):
        with tempfile.TemporaryDirectory(prefix="uu-broker-test-", dir="/private/tmp") as temporary:
            path = Path(temporary) / "terminal.sock"
            lock = Path(temporary) / "terminal.lock"
            with patch.object(terminal, "runtime_paths", return_value=(path, lock)), \
                    patch.object(terminal, "official_connection_active", return_value=True), \
                    patch.object(terminal.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(RuntimeError, "Official UU has an active"):
                    terminal.ensure_server("synthetic", Path("unused"), timeout=3)
                spawn.assert_not_called()

    def test_official_status_query_does_not_request_a_connection(self):
        response = subprocess.CompletedProcess([], 0, '{"success":true,"data":{"hasActiveConnections":true}}', "")
        with patch.object(terminal.subprocess, "run", return_value=response) as query:
            self.assertTrue(terminal.official_connection_active())
        self.assertEqual(query.call_args[0][0][1:], ["device", "status", "--json"])

    def test_unsafe_runtime_directories_are_rejected_without_modification(self):
        with tempfile.TemporaryDirectory(prefix="uu-broker-test-", dir="/private/tmp") as temporary:
            parent = Path(temporary)
            public = parent / "public"
            public.mkdir(mode=0o755)
            link = parent / "link"
            link.symlink_to(parent, target_is_directory=True)
            for path in [public, link]:
                with self.assertRaisesRegex(RuntimeError, "not private"):
                    terminal.private_directory(path)
            self.assertEqual(public.stat().st_mode & 0o777, 0o755)
            self.assertTrue(link.is_symlink())

    def test_ipc_rejects_oversize_and_truncated_payloads(self):
        for payload in [struct.pack("!I", terminal.MAX_FRAME + 1), struct.pack("!I", 10) + b"{}"]:
            reader, writer = socket.socketpair()
            try:
                writer.sendall(payload)
                writer.close()
                with self.assertRaises(RuntimeError):
                    terminal.receive(reader)
            finally:
                reader.close()
                writer.close()


if __name__ == "__main__":
    unittest.main()
