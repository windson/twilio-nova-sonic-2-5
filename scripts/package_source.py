"""Package one committed Git tree for a version-pinned CodeBuild build."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile

MANIFEST = "source-manifest.json"


def safe_name(name: str, *, directory: bool = False) -> str:
    value = name[:-1] if directory and name.endswith("/") else name
    parts = value.split("/")
    if (not value or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part in {"", ".", "..", ".git"} for part in parts)
            or PurePosixPath(value).is_absolute() or ":" in parts[0]):
        raise ValueError("Unsafe source archive path")
    if parts[0] == MANIFEST:
        raise ValueError("Source tree contains the reserved manifest path")
    return value


def package_source(repo: Path, revision: str, output: Path) -> dict[str, object]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Revision must be a full lowercase Git commit SHA")

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "--no-replace-objects", "-C", str(repo), *args])

    if git("rev-parse", "--verify", revision + "^{commit}").decode().strip() != revision:
        raise ValueError("Revision does not identify the expected commit")
    expected: dict[str, bool] = {}
    for record in git("ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, _ = metadata.split(b" ")
        name = safe_name(raw_name.decode("utf-8"))
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError("Source packages require regular files; links and submodules are unsupported")
        expected[name] = mode == b"100755"
    if not expected:
        raise ValueError("Cannot package an empty source tree")

    archive = io.BytesIO(git("archive", "--format=zip", revision))
    files: dict[str, dict[str, object]] = {}
    seen: set[str] = set()
    with zipfile.ZipFile(archive, "r") as source:
        for entry in source.infolist():
            name = safe_name(entry.filename, directory=entry.is_dir())
            if name in seen:
                raise ValueError("Duplicate source archive member")
            seen.add(name)
            mode = entry.external_attr >> 16
            if entry.is_dir():
                if stat.S_IFMT(mode) not in {0, stat.S_IFDIR}:
                    raise ValueError("Invalid directory in source archive")
                continue
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                raise ValueError("Nonregular source archive member")
            executable = bool(mode & 0o111)
            if name not in expected or executable != expected[name]:
                raise ValueError("Archive differs from the committed file list or modes")
            payload = source.read(entry)
            # export-subst and similar archive transformations must not silently
            # change reviewed source bytes.
            if payload != git("show", f"{revision}:{name}"):
                raise ValueError("Archive content differs from the committed source")
            files[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "executable": executable}
        if set(files) != set(expected):
            raise ValueError("Archive omits committed files")
        timestamp = source.infolist()[0].date_time

    manifest = (json.dumps({"schema_version": 1, "revision": revision, "files": files},
                           sort_keys=True, separators=(",", ":")) + "\n").encode()
    with zipfile.ZipFile(archive, "a", compression=zipfile.ZIP_DEFLATED) as target:
        entry = zipfile.ZipInfo(MANIFEST, timestamp)
        entry.create_system = 3
        entry.external_attr = (stat.S_IFREG | 0o644) << 16
        entry.compress_type = zipfile.ZIP_DEFLATED
        target.writestr(entry, manifest)
    data = archive.getvalue()
    # Never overwrite a release package, and keep the local artifact private.
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return {"revision": revision, "source_key": f"source/{revision}.zip",
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "archive_sha256": hashlib.sha256(data).hexdigest(),
            "file_count": len(files), "bytes": len(data)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = package_source(args.repo, args.revision, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Source packaging failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
