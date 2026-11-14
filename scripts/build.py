#!/usr/bin/env python3
"""Build the pinned UU protocol client with the macOS compatibility patch."""
import argparse
import fcntl
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def run(*arguments, cwd=None):
    subprocess.run(arguments, cwd=cwd, check=True)


def known_patches(record):
    """Prefer the recorded patch, then revisions shipped in this repository."""
    if record.is_file():
        yield record.read_bytes()
    history = subprocess.run(["git", "log", "-32", "--format=%H", "--", "macos-headless.patch"],
                             cwd=ROOT, capture_output=True, text=True)
    if history.returncode == 0:
        for commit in history.stdout.splitlines():
            patch = subprocess.run(["git", "show", commit + ":macos-headless.patch"],
                                   cwd=ROOT, capture_output=True)
            if patch.returncode == 0:
                yield patch.stdout


def apply_compatibility(source, patch, record, previous):
    """Upgrade only a known patch; retain unrelated edits and roll back conflicts."""
    def matches(path, reverse=False):
        arguments = ["git", "apply", "--check"] + (["--reverse"] if reverse else []) + [str(path)]
        return subprocess.run(arguments, cwd=source, capture_output=True).returncode == 0

    contents = patch.read_bytes()
    if matches(patch):
        run("git", "apply", str(patch), cwd=source)
    elif not matches(patch, reverse=True):
        for candidate in previous:
            if candidate == contents:
                continue
            with tempfile.NamedTemporaryFile(dir=source.parent, suffix=".patch") as saved:
                saved.write(candidate)
                saved.flush()
                old = Path(saved.name)
                if not matches(old, reverse=True):
                    continue
                run("git", "apply", "--reverse", str(old), cwd=source)
                try:
                    if not matches(patch):
                        raise RuntimeError("Cached local changes conflict with the updated patch")
                    run("git", "apply", str(patch), cwd=source)
                except BaseException:
                    run("git", "apply", str(old), cwd=source)
                    raise
                break
        else:
            raise RuntimeError("Build checkout has unknown changes; it was preserved without modification")
    temporary = record.with_suffix(".new")
    temporary.write_bytes(contents)
    temporary.replace(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true", help="Use Cargo's release profile.")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("This launcher currently supports macOS only")
    for command in ["git", "cargo", "swift"]:
        if shutil.which(command) is None:
            raise RuntimeError(f"Required command is missing: {command}")
    cache = ROOT / ".build"
    cache.mkdir(exist_ok=True)
    with (cache / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        build(args.release)


def build(release):
    repository, commit = (ROOT / "UPSTREAM.txt").read_text().splitlines()[:2]
    if repository != "https://github.com/VallierDev/uu-remote-linux" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Unexpected upstream source configuration")
    source = ROOT / ".build/upstream"
    source.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").exists():
        run("git", "init", cwd=source)
        run("git", "remote", "add", "origin", repository, cwd=source)
        run("git", "fetch", "--depth=1", "origin", commit, cwd=source)
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=source)
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if current != commit:
        raise RuntimeError("Existing build checkout differs from the pinned upstream commit")

    record = ROOT / ".build/applied-headless.patch"
    apply_compatibility(source, ROOT / "macos-headless.patch", record, known_patches(record))

    build = ["cargo", "build", "--locked", "--features", "native-viewer", "--bin", "uuremote-viewer"]
    if release:
        build.append("--release")
    run(*build, cwd=source)
    profile = "release" if release else "debug"
    destination = ROOT / "bin"
    destination.mkdir(exist_ok=True)
    temporary = destination / "uuremote-viewer.new"
    shutil.copy2(source / "target" / profile / "uuremote-viewer", temporary)
    temporary.replace(destination / "uuremote-viewer")
    print("Built:", destination / "uuremote-viewer")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
