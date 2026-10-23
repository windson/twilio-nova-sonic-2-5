#!/usr/bin/env bash
# The template authenticates this script against an independently pinned manifest
# before executing it. Isolated Python never imports modules from the archive.
set -euo pipefail
python3 -I -S - <<'PYVERIFY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


def fail(message):
    sys.exit(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify():
    revision = os.environ.get("SOURCE_REVISION", "")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        fail("SOURCE_REVISION must be a full lowercase Git commit SHA.")
    bucket = os.environ.get("SOURCE_ARCHIVE_BUCKET", "")
    key = os.environ.get("SOURCE_ARCHIVE_KEY", "")
    version = os.environ.get("SOURCE_ARCHIVE_VERSION_ID", "")
    if not bucket or key != f"source/{revision}.zip":
        fail("Source archive key and reviewed Git revision do not match.")
    if not version or version == "null" or os.environ.get("CODEBUILD_SOURCE_VERSION") != version:
        fail("CodeBuild did not download the pinned S3 object version.")
    # AWS documents s3://bucket/key; the native S3 downloader also reports
    # bucket/key. Accept only these exact representations of the pinned object.
    source_locations = {f"{bucket}/{key}", f"s3://{bucket}/{key}"}
    if os.environ.get("CODEBUILD_SOURCE_REPO_URL") not in source_locations:
        fail("CodeBuild source location does not match the pinned archive.")
    manifest_hash = os.environ.get("SOURCE_MANIFEST_SHA256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_hash):
        fail("SOURCE_MANIFEST_SHA256 must be independently pinned.")
    manifest_path = Path("source-manifest.json")
    if not stat.S_ISREG(manifest_path.lstat().st_mode):
        fail("Source manifest must be a regular file.")
    raw = manifest_path.read_bytes()
    if digest(raw) != manifest_hash:
        fail("Source manifest does not match the independently pinned digest.")
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "revision", "files"}
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
            or manifest["revision"] != revision):
        fail("Source manifest schema or reviewed revision does not match.")
    files = manifest["files"]
    required = {"Dockerfile", ".dockerignore", "infra/verify_source.sh", "infra/build_image.sh"}
    if not isinstance(files, dict) or not required <= files.keys():
        fail("Source manifest is missing required build inputs.")
    expected_dirs = set()
    for name, entry in files.items():
        if (not isinstance(name, str) or not name or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or any(part in {"", ".", "..", ".git"} for part in name.split("/"))
                or ":" in name.split("/")[0]
                or name.split("/")[0] == manifest_path.name):
            fail("Source manifest contains an unsafe or reserved path.")
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "executable"}
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or type(entry["executable"]) is not bool):
            fail("Source manifest contains an invalid file record.")
        expected_dirs.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")

    actual_files, actual_dirs = set(), set()
    for base, directories, names in os.walk(".", followlinks=False):
        for name in directories:
            path = Path(base) / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                fail("Source archive contains a directory link or special entry.")
            actual_dirs.add(path.as_posix())
        for name in names:
            path = Path(base) / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or mode & 0o7000 or path.lstat().st_nlink != 1:
                fail("Source archive contains a link or special file.")
            if path == manifest_path:
                continue
            actual_files.add(path.as_posix())
    if actual_files != set(files) or actual_dirs != expected_dirs:
        fail("Source archive file set does not match the pinned manifest.")
    for name, entry in files.items():
        path = Path(name)
        if digest(path.read_bytes()) != entry["sha256"]:
            fail("Source archive file content does not match the pinned manifest.")
        if bool(path.stat().st_mode & 0o111) != entry["executable"]:
            fail("Source archive executable mode does not match the pinned manifest.")
    print(f"Verified source archive for Git commit {revision} ({len(files)} files).")


try:
    verify()
except (OSError, ValueError, KeyError, TypeError):
    fail("Source archive verification failed; refusing to build.")
PYVERIFY
