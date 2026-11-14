#!/usr/bin/env python3
"""Share one headless UU connection across local terminals and management calls."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
MAX_FRAME = 4 * 1024 * 1024 + 1024


def receive(connection):
    """Read a bounded, length-prefixed IPC reply; no credentials use this path."""
    def exact(length):
        result = bytearray()
        while len(result) < length:
            block = connection.recv(length - len(result))
            if not block:
                raise RuntimeError("The shared UU connection ended")
            result.extend(block)
        return bytes(result)

    length = struct.unpack("!I", exact(4))[0]
    if length > MAX_FRAME:
        raise RuntimeError("Invalid shared terminal response size")
    return json.loads(exact(length))


def request(path, message, timeout=40):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        payload = json.dumps(message, separators=(",", ":")).encode()
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        reply = receive(connection)
    if isinstance(reply, dict) and "Failure" in reply:
        raise RuntimeError(reply["Failure"])
    return reply


def available(path):
    try:
        reply = request(path, "Ping", timeout=0.3)
    except (OSError, RuntimeError, ValueError):
        return False
    if reply != {"Pong": 1}:
        raise RuntimeError("Shared terminal version changed; close its windows and wait 10 seconds before retrying")
    return True


def official_connection_active():
    """Check the official app without opening a desktop or requesting capture."""
    result = subprocess.run(
        ["/Applications/UURemote.app/Contents/Helpers/uuyc-cli", "device", "status", "--json"],
        capture_output=True, text=True, timeout=8,
    )
    if result.returncode:
        raise RuntimeError("Cannot confirm the official UU connection state")
    response = json.loads(result.stdout)
    active = response.get("data", {}).get("hasActiveConnections")
    if response.get("success") is not True or not isinstance(active, bool):
        raise RuntimeError("Cannot confirm the official UU connection state")
    return active


def private_directory(directory):
    """Reject unsafe existing directories without changing their permissions."""
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise RuntimeError("The shared terminal directory is not private")


def runtime_paths(device):
    """Keep the socket below macOS's path limit and inaccessible to other users."""
    directory = Path("/private/tmp") / f"uu-headless-{os.geteuid()}"
    private_directory(directory)
    key = hashlib.sha256(device.encode()).hexdigest()[:20]
    return directory / (key + ".sock"), directory / (key + ".lock")


def ensure_server(device, executable, timeout=65):
    """Reuse a live broker or launch one under an inherited per-device lock.

    Closing our copy of the descriptor keeps the child's lock alive. Never
    explicitly unlock it: the child shares the same open-file description.
    """
    path, lock_path = runtime_paths(device)
    if available(path):
        return path
    deadline = time.monotonic() + timeout
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    child = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RuntimeError("The shared terminal lock is not private")
        while time.monotonic() < deadline:
            if available(path):
                return path
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for the shared UU connection")
        # Another launcher may have completed startup before we obtained the lock.
        if available(path):
            return path
        if official_connection_active():
            raise RuntimeError("Official UU has an active remote connection; a separate terminal connection was not started")
        environment = dict(os.environ, UU_TERMINAL_LOCK_FD=str(descriptor))
        child = subprocess.Popen(
            ["/usr/bin/swift", str(ROOT / "scripts/credentials.swift"), str(executable),
             "terminal-server", device, str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=environment, pass_fds=(descriptor,), start_new_session=True,
        )
        while time.monotonic() < deadline:
            if available(path):
                return path
            if child.poll() is not None:
                errors = child.stderr.read(32768).decode(errors="replace")
                detail = errors.strip().splitlines()[-1:] or ["UU connection startup failed"]
                raise RuntimeError(detail[0])
            time.sleep(0.1)
        raise RuntimeError("Timed out starting the shared UU connection")
    finally:
        os.close(descriptor)
        if child is not None and child.stderr is not None:
            child.stderr.close()


def session_id(value):
    number = int(value)
    if not 0 < number <= 0xffffffff:
        raise argparse.ArgumentTypeError("Session ID must be between 1 and 4294967295")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="UU device ID; defaults to config.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--session", type=session_id, help="Attach to an existing session")
    modes.add_argument("--list", action="store_true", help="List sessions without opening a shell")
    modes.add_argument("--kill", type=session_id, nargs="+", help="End selected remote sessions")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("This launcher currently supports macOS only")
    device = args.device or json.loads((ROOT / "config.json").read_text())["uu_device_id"]
    if not isinstance(device, str) or not re.fullmatch(r"[a-z0-9]{16}", device):
        raise ValueError("Invalid UU device ID")
    executable = ROOT / "bin/uuremote-viewer"
    if not executable.is_file():
        raise RuntimeError("Build the client first: python3 scripts/build.py")
    path = ensure_server(device, executable)
    if args.list:
        reply = request(path, "List")
        if not isinstance(reply, dict) or not isinstance(reply.get("Sessions"), list):
            raise RuntimeError("Invalid session-list reply")
        print(json.dumps(reply["Sessions"], ensure_ascii=False))
        return 0
    if args.kill:
        reply = request(path, {"Kill": args.kill})
        if not isinstance(reply, dict) or reply.get("Killed") != args.kill:
            raise RuntimeError("Session termination was not acknowledged")
        for identifier in args.kill:
            print(json.dumps({"killed": identifier}))
        return 0
    command = [str(executable), "terminal-local", str(path)]
    if args.session is not None:
        command.append(str(args.session))
    os.execv(command[0], command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        # Errors contain local state or bounded client errors, never credentials.
        print(f"UU terminal: {error}", file=sys.stderr)
        raise SystemExit(1)
