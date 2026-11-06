#!/usr/bin/env python3
"""Build the pinned UU protocol client with the macOS compatibility patch."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def run(*arguments, cwd=None):
    subprocess.run(arguments, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true", help="Use Cargo's release profile.")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("This launcher currently supports macOS only")
    for command in ["git", "cargo", "swift"]:
        if shutil.which(command) is None:
            raise RuntimeError(f"Required command is missing: {command}")

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

    patch = str(ROOT / "macos-headless.patch")
    clean = subprocess.run(["git", "apply", "--check", patch], cwd=source, capture_output=True)
    if clean.returncode == 0:
        run("git", "apply", patch, cwd=source)
    elif subprocess.run(["git", "apply", "--reverse", "--check", patch], cwd=source, capture_output=True).returncode:
        raise RuntimeError("Compatibility patch cannot be applied to the build checkout")

    build = ["cargo", "build", "--locked", "--features", "native-viewer", "--bin", "uuremote-viewer"]
    if args.release:
        build.append("--release")
    run(*build, cwd=source)
    profile = "release" if args.release else "debug"
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
