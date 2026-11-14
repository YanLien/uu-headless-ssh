"""Upgrade public patches in an isolated Git checkout without fetching or building."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.build import apply_compatibility


class PatchUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="uu-patch-test-", dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.file = self.source / "sample.txt"
        self.base = ["base\n"] + [f"padding {index}\n" for index in range(30)] + ["original tail\n"]
        self.file.write_text("".join(self.base))
        self.git("add", "sample.txt")
        self.git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        self.old = self.make_patch("old", change_tail=False)
        self.new = self.make_patch("new", change_tail=False)
        self.patch = self.root / "current.patch"
        self.record = self.root / "record.patch"

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.source, stderr=subprocess.PIPE)

    def make_patch(self, version, change_tail):
        lines = self.base.copy()
        lines[0] = version + "\n"
        if change_tail:
            lines[-1] = "updated tail\n"
        self.file.write_text("".join(lines))
        patch = self.git("diff", "--", "sample.txt")
        self.file.write_text("".join(self.base))
        return patch

    def prepare_old(self):
        self.patch.write_bytes(self.old)
        apply_compatibility(self.source, self.patch, self.record, [])
        lines = self.file.read_text().splitlines(keepends=True)
        lines[-1] = "user tail\n"
        self.file.write_text("".join(lines))

    def test_known_patch_upgrade_preserves_unrelated_local_edits(self):
        self.prepare_old()
        self.patch.write_bytes(self.new)
        apply_compatibility(self.source, self.patch, self.record, [self.old])
        lines = self.file.read_text().splitlines()
        self.assertEqual(lines[0], "new")
        self.assertEqual(lines[-1], "user tail")
        self.assertEqual(self.record.read_bytes(), self.new)

    def test_conflicting_upgrade_restores_the_previous_patch_and_user_edits(self):
        self.new = self.make_patch("new", change_tail=True)
        self.prepare_old()
        before = self.git("diff")
        self.patch.write_bytes(self.new)
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            apply_compatibility(self.source, self.patch, self.record, [self.old])
        self.assertEqual(self.git("diff"), before)
        self.assertEqual(self.record.read_bytes(), self.old)

    def test_unknown_edits_are_not_reset(self):
        self.prepare_old()
        self.file.write_text(self.file.read_text().replace("old\n", "custom\n", 1))
        before = self.git("diff")
        self.patch.write_bytes(self.new)
        with self.assertRaisesRegex(RuntimeError, "unknown changes"):
            apply_compatibility(self.source, self.patch, self.record, [self.old])
        self.assertEqual(self.git("diff"), before)


if __name__ == "__main__":
    unittest.main()
