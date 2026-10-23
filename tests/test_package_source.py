"""Release packages must contain the committed tree, never local state."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from scripts.package_source import MANIFEST, package_source, safe_name


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.py").write_text("print('committed')\n")
    (path / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (path / "run.sh").chmod(0o755)
    commit(path)
    return path


def commit(path):
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Package test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def revision(path):
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_package_uses_committed_bytes_and_excludes_local_files(repo, tmp_path):
    sha = revision(repo)
    (repo / "app.py").write_text("uncommitted edit\n")
    (repo / ".env").write_text("untracked local configuration\n")
    output = tmp_path / "source.zip"
    result = package_source(repo, sha, output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"app.py", "run.sh", MANIFEST}
        assert archive.read("app.py") == b"print('committed')\n"
        raw = archive.read(MANIFEST)
        manifest = json.loads(raw)
        assert manifest["revision"] == sha
        assert manifest["files"]["run.sh"]["executable"] is True
        assert manifest["files"]["app.py"]["executable"] is False
        assert manifest["files"]["app.py"]["sha256"] == hashlib.sha256(archive.read("app.py")).hexdigest()
        assert result["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["archive_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["source_key"] == f"source/{sha}.zip"
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        package_source(repo, sha, output)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../b", "a//b", "a\\b", ".git/config", "C:/file", "line\nfile", "tab\tfile", "del\x7ffile", MANIFEST, MANIFEST + "/file"])
def test_invalid_archive_names_are_rejected(name):
    with pytest.raises(ValueError):
        safe_name(name)


def test_committed_symlink_is_rejected_before_writing(repo, tmp_path):
    (repo / "link").symlink_to("app.py")
    sha = commit(repo)
    output = tmp_path / "source.zip"
    with pytest.raises(ValueError, match="regular files"):
        package_source(repo, sha, output)
    assert not output.exists()


def test_reserved_manifest_collision_is_rejected(repo, tmp_path):
    (repo / MANIFEST).write_text("{}")
    sha = commit(repo)
    with pytest.raises(ValueError, match="reserved"):
        package_source(repo, sha, tmp_path / "source.zip")


def test_git_archive_transformations_cannot_change_committed_source(repo, tmp_path):
    (repo / ".gitattributes").write_text("app.py export-ignore\n")
    sha = commit(repo)
    with pytest.raises(ValueError, match="omits committed files"):
        package_source(repo, sha, tmp_path / "source.zip")


def test_submodule_is_rejected_before_archiving(repo, tmp_path):
    sha = revision(repo)
    subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                    f"160000,{sha},nested"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Package test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "gitlink"], check=True)
    with pytest.raises(ValueError, match="regular files"):
        package_source(repo, revision(repo), tmp_path / "source.zip")


def test_local_replace_refs_cannot_relabel_another_committed_tree(repo, tmp_path):
    original = revision(repo)
    (repo / "app.py").write_text("print('replacement')\n")
    replacement = commit(repo)
    subprocess.run(["git", "-C", str(repo), "replace", original, replacement], check=True)
    output = tmp_path / "source.zip"
    result = package_source(repo, original, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("app.py") == b"print('committed')\n"
        assert json.loads(archive.read(MANIFEST))["revision"] == original
    assert result["revision"] == original
