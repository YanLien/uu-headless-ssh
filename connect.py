#!/usr/bin/env python3
"""Connect through UU to Windows, then run that Windows host's SSH client."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty

ROOT = Path(__file__).resolve().parent
ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def sync_terminal_size(master, source=0):
    """Propagate Ghostty's size to the nested PTY; use a fallback for pipes."""
    try:
        cols, rows = os.get_terminal_size(source)
    except OSError:
        cols, rows = 120, 40
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))


def stop_client(pid, master, forward=False, grace=30):
    """Keep the PTY alive while the client deletes its owned remote terminal."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    can_read = True
    while True:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            return os.waitstatus_to_exitcode(status)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.kill(pid, signal.SIGKILL)
            return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        ready, _, _ = select.select([master] if can_read else [], [], [], min(0.2, remaining))
        if ready:
            try:
                block = os.read(master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                block = b""
            if not block:
                can_read = False
            if forward and block:
                sys.stdout.buffer.write(block)
                sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify B-to-C SSH and return JSON.")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    device = config["uu_device_id"]
    host = config["ssh_host"]
    user = config["ssh_user"]
    port = int(config["ssh_port"])
    if not (re.fullmatch(r"[a-z0-9]{16}", device)
            and re.fullmatch(r"[A-Za-z0-9_.:-]+", host)
            and re.fullmatch(r"[A-Za-z0-9_.-]+", user)
            and 1 <= port <= 65535):
        raise ValueError("Invalid UU or SSH target configuration")

    # A real local PTY lets the existing UU terminal protocol carry stdin/stdout
    # without a desktop window, screen capture, accessibility API or mouse input.
    pid, master = pty.fork()
    if pid == 0:
        command = ["/usr/bin/swift", str(ROOT / "scripts/credentials.swift"),
                   str(ROOT / "bin/uuremote-viewer"), "terminal", device]
        os.execv(command[0], command)
    saved = None
    previous_resize = None
    data = bytearray()
    sent = False
    expired = False
    deadline = time.monotonic() + 35
    target = f"{user}@{host}"
    ssh = f"ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 -p {port}"
    if args.check:
        # Build the result marker in PowerShell so echoed input cannot pass the
        # check before the actual SSH process has returned its exit status.
        command = (f"{ssh} {target} 'hostname; id -un; uname -s'; "
                   "$uuExit=$LASTEXITCODE; Write-Output ('__UU_CHECK_' + $uuExit + '__'); exit $uuExit\r")
    else:
        # Ending Linux must also end this task's PowerShell, so the protocol
        # client can delete its owned terminal instead of leaving an idle shell.
        command = f"{ssh} -tt {target}; $uuExit=$LASTEXITCODE; exit $uuExit\r"

    try:
        # Setup failures still need to stop the already-started child. Keep
        # both geometry and raw-mode setup inside the cleanup scope.
        sync_terminal_size(master)
        previous_resize = signal.signal(signal.SIGWINCH, lambda _signal, _frame: sync_terminal_size(master))
        saved = termios.tcgetattr(0) if os.isatty(0) and not args.check else None
        if saved is not None:
            tty.setraw(0)
        while True:
            if (args.check or not sent) and time.monotonic() > deadline:
                expired = True
                break
            inputs = [master] + ([] if args.check else [0])
            ready, _, _ = select.select(inputs, [], [], 0.2)
            if master in ready:
                try:
                    block = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not block:
                    break
                data.extend(block)
                if len(data) > 1024 * 1024:
                    del data[:-1024 * 1024]
                if not args.check:
                    sys.stdout.buffer.write(block)
                    sys.stdout.buffer.flush()
                text = ANSI.sub(b"", bytes(data))
                if not sent and re.search(rb"PS[^\r\n>]{0,256}>", text):
                    os.write(master, command.encode())
                    sent = True
            if not args.check and 0 in ready:
                block = os.read(0, 16384)
                if not block or b"\x1d" in block:
                    break
                os.write(master, block)
    finally:
        if previous_resize is not None:
            signal.signal(signal.SIGWINCH, previous_resize)
        if saved is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, saved)
            except termios.error:
                pass  # Closing the outer window must not skip remote cleanup.
        try:
            child_status = stop_client(pid, master, forward=not args.check)
        finally:
            os.close(master)

    if args.check:
        text = ANSI.sub(b"", bytes(data))
        identity = re.search(rb"(?:\r?\n)([A-Za-z0-9_.-]+)\r?\n([A-Za-z0-9_.-]+)\r?\nLinux\r?\n", text)
        verified = b"__UU_CHECK_0__" in text and identity is not None
        success = not expired and verified and child_status == 0
        report = {"ok": success, "via_uu_device": device, "ssh_target": target}
        if success:
            report.update(hostname=identity[1].decode(), user=identity[2].decode(), system="Linux", ssh_exit_code=0)
        else:
            report["error"] = ("Timed out" if expired else "UU client exited with an error"
                               if verified else "SSH identity verification failed")
            report["client_exit_code"] = child_status
        print(json.dumps(report, ensure_ascii=False))
        return 0 if success else 1
    if expired:
        print("Timed out waiting for the Windows terminal.", file=sys.stderr)
        return 1
    return child_status if child_status >= 0 else 128 - child_status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError):
        print("UU connection setup failed; no credentials displayed.", file=sys.stderr)
        raise SystemExit(1)
