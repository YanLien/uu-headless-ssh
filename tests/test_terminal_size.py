"""Verify the bridge's geometry using local kernel PTYs, without a UU session."""
import fcntl
import os
import pty
import struct
import termios
import unittest

from connect import sync_terminal_size


class TerminalSizeTests(unittest.TestCase):
    def test_resizing_the_outer_terminal_reaches_the_child_pty(self):
        """A second update must replace the initial geometry seen by the child."""
        outer, source = pty.openpty()
        master, child = pty.openpty()
        try:
            for rows, cols in [(31, 97), (54, 163)]:
                fcntl.ioctl(outer, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                sync_terminal_size(master, source)
                self.assertEqual(os.get_terminal_size(child), (cols, rows))
        finally:
            for descriptor in [outer, source, master, child]:
                os.close(descriptor)

    def test_piped_input_uses_a_usable_fallback_size(self):
        """Codex can launch the bridge with a pipe instead of a local TTY."""
        reader, writer = os.pipe()
        master, child = pty.openpty()
        try:
            sync_terminal_size(master, reader)
            self.assertEqual(os.get_terminal_size(child), (120, 40))
        finally:
            for descriptor in [reader, writer, master, child]:
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
