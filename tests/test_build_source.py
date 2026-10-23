"""Authenticate version-pinned source archives before any ECR or Docker action."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class CloudFormationLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag: value}


CloudFormationLoader.add_multi_constructor("!", intrinsic)


def template():
    return yaml.load((ROOT / "infra/template.yaml").read_text(), Loader=CloudFormationLoader)


def bootstrap_script():
    source = template()["Resources"]["ImageBuildProject"]["Properties"]["Source"]
    assert source["Type"] == "S3"
    return yaml.safe_load(source["BuildSpec"])["phases"]["build"]["commands"][0]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def save_manifest(archive, *, pin=False):
    raw = (json.dumps(archive["manifest"], sort_keys=True, separators=(",", ":")) + "\n").encode()
    (archive["source"] / "source-manifest.json").write_bytes(raw)
    if pin:
        archive["env"]["SOURCE_MANIFEST_SHA256"] = hashlib.sha256(raw).hexdigest()


@pytest.fixture
def archive(tmp_path):
    # Commit real files first. Package Git's immutable commit, never the worktree.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)], check=True)
    (repo / "infra").mkdir()
    for name in ["verify_source.sh", "build_image.sh", "buildspec.yml"]:
        (repo / "infra" / name).write_bytes((ROOT / "infra" / name).read_bytes())
    (repo / "Dockerfile").write_text("FROM scratch\n")
    (repo / ".dockerignore").write_text("infra/\n")
    (repo / "app.py").write_text("print('committed code')\n")
    (repo / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "run.sh").chmod(0o755)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Source Test", "-c", "user.email=test@example.invalid",
        "commit", "--quiet", "-m", "Source fixture")
    revision = git(repo, "rev-parse", "HEAD")
    zipped = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=zip", revision])
    source = tmp_path / "source"
    source.mkdir()
    files = {}
    with zipfile.ZipFile(io.BytesIO(zipped)) as package:
        package.extractall(source)
        for member in package.infolist():
            if member.is_dir():
                continue
            mode = member.external_attr >> 16
            # Git omits Unix mode metadata for ordinary non-executable files.
            (source / member.filename).chmod(mode & 0o777 if mode else 0o644)
            files[member.filename] = {"sha256": hashlib.sha256(package.read(member)).hexdigest(),
                                      "executable": bool(mode & 0o111)}
    # A stray/uncommitted developer secret cannot enter the Git archive.
    (repo / ".env").write_text("fixture-private-value")
    (repo / "app.py").write_text("uncommitted change\n")
    env = dict(os.environ, SOURCE_REVISION=revision, SOURCE_ARCHIVE_BUCKET="fixture-source-bucket",
               SOURCE_ARCHIVE_KEY=f"source/{revision}.zip", SOURCE_ARCHIVE_VERSION_ID="fixture-version-1",
               CODEBUILD_SOURCE_VERSION="fixture-version-1",
               CODEBUILD_SOURCE_REPO_URL=f"s3://fixture-source-bucket/source/{revision}.zip",
               AWS_DEFAULT_REGION="us-east-1", AWS_ACCOUNT_ID="123456789012", ECR_REPO="fixture-app",
               IMAGE_TAG=revision)
    # S3 has no resolved Git SHA; an unrelated value cannot be used as attestation.
    env["CODEBUILD_RESOLVED_SOURCE_VERSION"] = "not-a-git-revision"
    value = {"source": source, "repo": repo, "revision": revision, "env": env,
             "manifest": {"schema_version": 1, "revision": revision, "files": files}}
    save_manifest(value, pin=True)
    return value


def verify(archive):
    return subprocess.run(["bash", str(ROOT / "infra/verify_source.sh")], cwd=archive["source"],
                          env=archive["env"], capture_output=True, text=True)


def mock_build_tools(archive, tmp_path):
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    audit = tmp_path / "build-actions.jsonl"
    shim = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["FIXTURE_BUILD_AUDIT"], "a") as f:
    f.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "aws":
    print("fixture-registry-password")
elif len(sys.argv) > 1 and sys.argv[1] == "login":
    sys.stdin.read()
'''
    for name in ["aws", "docker"]:
        executable = shim_dir / name
        executable.write_text(shim)
        executable.chmod(0o755)
    archive["env"].update(PATH=f"{shim_dir}:{os.environ['PATH']}", FIXTURE_BUILD_AUDIT=str(audit))
    return audit


def bootstrap(archive):
    return subprocess.run(["bash", "-c", bootstrap_script()], cwd=archive["source"],
                          env=archive["env"], capture_output=True, text=True)


@pytest.mark.parametrize("location_prefix", ["", "s3://"])
def test_committed_archive_passes_independent_manifest_file_and_version_checks(archive, location_prefix):
    archive["env"]["CODEBUILD_SOURCE_REPO_URL"] = (
        f"{location_prefix}{archive['env']['SOURCE_ARCHIVE_BUCKET']}/{archive['env']['SOURCE_ARCHIVE_KEY']}"
    )
    result = verify(archive)
    assert result.returncode == 0, result.stderr
    assert archive["revision"] in result.stdout
    assert not (archive["source"] / ".env").exists()
    assert (archive["source"] / "app.py").read_text() == "print('committed code')\n"


@pytest.mark.parametrize("location", [
    "other-bucket/{key}", "s3://other-bucket/{key}",
    "{bucket}/source/other.zip", "s3://{bucket}/source/other.zip",
    "https://{bucket}/{key}", "prefix/s3://{bucket}/{key}", "S3://{bucket}/{key}",
    "{bucket}/{key}?versionId=fixture-version-1", "s3://{bucket}/{key}?query=value",
    "s3://{bucket}/{key}#fragment", "{bucket}/{key}/", "s3://{bucket}//{key}",
])
def test_source_location_rejects_changed_bucket_key_prefix_or_url_suffix(archive, location):
    archive["env"]["CODEBUILD_SOURCE_REPO_URL"] = location.format(
        bucket=archive["env"]["SOURCE_ARCHIVE_BUCKET"], key=archive["env"]["SOURCE_ARCHIVE_KEY"],
    )
    result = verify(archive)
    assert result.returncode != 0
    assert "source location does not match" in result.stderr


@pytest.mark.parametrize("revision", ["", "main", "abc1234", "A" * 40, "a" * 39, "a" * 41])
def test_full_lowercase_git_sha_required(archive, revision):
    archive["env"]["SOURCE_REVISION"] = revision
    result = verify(archive)
    assert result.returncode != 0
    assert "SOURCE_REVISION" in result.stderr


@pytest.mark.parametrize(("variable", "bad_value", "error"), [
    ("SOURCE_ARCHIVE_KEY", "source/other.zip", "key"),
    ("SOURCE_ARCHIVE_VERSION_ID", "", "version"),
    ("SOURCE_ARCHIVE_VERSION_ID", "null", "version"),
    ("CODEBUILD_SOURCE_VERSION", "", "version"),
    ("CODEBUILD_SOURCE_VERSION", "different-version", "version"),
    ("CODEBUILD_SOURCE_REPO_URL", "s3://other/source.zip", "location"),
    ("SOURCE_MANIFEST_SHA256", "", "independently pinned"),
    ("SOURCE_MANIFEST_SHA256", "0" * 64, "pinned digest"),
])
def test_expected_archive_identity_cannot_drift(archive, variable, bad_value, error):
    archive["env"][variable] = bad_value
    result = verify(archive)
    assert result.returncode != 0
    assert error in result.stderr


def test_manifest_must_describe_expected_revision_even_with_trusted_digest(archive):
    archive["manifest"]["revision"] = "0" * 40
    save_manifest(archive, pin=True)
    result = verify(archive)
    assert result.returncode != 0
    assert "revision does not match" in result.stderr


def test_self_consistent_modified_archive_cannot_replace_external_manifest_pin(archive, tmp_path):
    audit = mock_build_tools(archive, tmp_path)
    (archive["source"] / "app.py").write_text("different source\n")
    archive["manifest"]["files"]["app.py"]["sha256"] = hashlib.sha256(b"different source\n").hexdigest()
    save_manifest(archive)  # Attacker controls ZIP/manifest, not CloudFormation pin.
    result = bootstrap(archive)
    assert result.returncode != 0
    assert "Source trust bootstrap failed" in result.stderr
    assert not audit.exists()


@pytest.mark.parametrize("change", ["content", "missing", "extra", "hidden", "empty_dir", "executable"])
def test_all_files_and_executable_flags_must_match_manifest(archive, change):
    source = archive["source"]
    if change == "content":
        (source / "app.py").write_text("tampered\n")
    elif change == "missing":
        (source / "app.py").unlink()
    elif change == "extra":
        (source / "unlisted.py").write_text("extra\n")
    elif change == "hidden":
        (source / ".env").write_text("fixture-private-value")
    elif change == "empty_dir":
        (source / "unlisted").mkdir()
    elif change == "executable":
        (source / "app.py").chmod(0o755)
    result = verify(archive)
    assert result.returncode != 0
    assert "does not match" in result.stderr
    assert "fixture-private-value" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/../b", "a//b", "a\\b", ".git/config", "source-manifest.json"])
def test_unsafe_or_reserved_manifest_paths_rejected(archive, name):
    archive["manifest"]["files"][name] = {"sha256": "0" * 64, "executable": False}
    save_manifest(archive, pin=True)
    result = verify(archive)
    assert result.returncode != 0
    assert "unsafe or reserved" in result.stderr


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "hardlink", "fifo"])
def test_links_and_special_files_rejected(archive, tmp_path, kind):
    source = archive["source"]
    if kind == "file_link":
        (source / "app.py").unlink()
        (source / "app.py").symlink_to(archive["repo"] / "app.py")
    elif kind == "directory_link":
        (source / "linked").symlink_to(archive["repo"], target_is_directory=True)
    elif kind == "hardlink":
        os.link(source / "app.py", tmp_path / "outside-hardlink")
    else:
        os.mkfifo(source / "pipe")
    result = verify(archive)
    assert result.returncode != 0
    assert "link or special" in result.stderr


def test_untrusted_verifier_is_rejected_before_execution(archive, tmp_path):
    audit = mock_build_tools(archive, tmp_path)
    marker = tmp_path / "untrusted-executed"
    (archive["source"] / "infra/verify_source.sh").write_text(f"touch '{marker}'\n")
    result = bootstrap(archive)
    assert result.returncode != 0
    assert "Source trust bootstrap failed" in result.stderr
    assert not marker.exists()
    assert not audit.exists()


@pytest.mark.parametrize("module", ["hashlib.py", "json.py", "sitecustomize.py"])
def test_archive_modules_cannot_run_during_trusted_bootstrap(archive, tmp_path, module):
    audit = mock_build_tools(archive, tmp_path)
    marker = tmp_path / "module-executed"
    (archive["source"] / module).write_text(f"open({str(marker)!r}, 'w').write('executed')\n")
    archive["env"]["PYTHONPATH"] = str(archive["source"])
    result = bootstrap(archive)
    assert result.returncode != 0  # Extra module fails exact-file verification.
    assert not marker.exists()
    assert not audit.exists()


def test_valid_archive_reaches_ecr_then_builds_verified_revision(archive, tmp_path):
    audit = mock_build_tools(archive, tmp_path)
    result = bootstrap(archive)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(calls) == 4
    assert sorted(call[:2] for call in calls[:2]) == [["aws", "ecr"], ["docker", "login"]]
    assert calls[2][:2] == ["docker", "build"]
    assert f"BUILD_REVISION={archive['revision']}" in calls[2]
    assert calls[3][:2] == ["docker", "push"]
    assert "fixture-registry-password" not in result.stdout + result.stderr
    assert f"Pushed image for verified Git commit {archive['revision']}" in result.stdout


def test_release_packager_output_matches_codebuild_verifier_contract(archive, tmp_path):
    package_path = tmp_path / "release.zip"
    packaged = subprocess.run(
        [sys.executable, str(ROOT / "scripts/package_source.py"), "--repo", str(archive["repo"]),
         "--revision", archive["revision"], "--output", str(package_path)],
        check=True, text=True, capture_output=True,
    )
    metadata = json.loads(packaged.stdout)
    extracted = tmp_path / "release"
    with zipfile.ZipFile(package_path) as package:
        package.extractall(extracted)
        for member in package.infolist():
            if not member.is_dir():
                mode = member.external_attr >> 16
                (extracted / member.filename).chmod(mode & 0o777 if mode else 0o644)
    archive["source"] = extracted
    archive["env"]["SOURCE_MANIFEST_SHA256"] = metadata["manifest_sha256"]
    archive["env"]["SOURCE_ARCHIVE_KEY"] = metadata["source_key"]
    # Exercise the bare location observed from CodeBuild's native S3 downloader.
    archive["env"]["CODEBUILD_SOURCE_REPO_URL"] = f"{archive['env']['SOURCE_ARCHIVE_BUCKET']}/{metadata['source_key']}"
    audit = mock_build_tools(archive, tmp_path)
    result = bootstrap(archive)
    assert result.returncode == 0, result.stderr
    assert metadata["revision"] == archive["revision"]
    assert len(audit.read_text().splitlines()) == 4
    assert not (extracted / ".env").exists()


def test_only_version_pinned_s3_source_and_object_permission_remain():
    document = template()
    params = document["Parameters"]
    assert not any(name.startswith("GitLab") or name == "GitHubRepoUrl" for name in params)
    for name in ["SourceArchiveBucket", "SourceArchiveKey", "SourceArchiveVersionId", "SourceVersion", "SourceManifestSha256"]:
        assert "Default" not in params[name]
    assert params["SourceVersion"]["AllowedPattern"] == "^[a-f0-9]{40}$"
    assert params["SourceManifestSha256"]["AllowedPattern"] == "^[a-f0-9]{64}$"
    project = document["Resources"]["ImageBuildProject"]["Properties"]
    assert project["Source"]["Type"] == "S3"
    assert project["Source"]["Location"] == {"Sub": "${SourceArchiveBucket}/${SourceArchiveKey}"}
    assert project["SourceVersion"] == {"Ref": "SourceArchiveVersionId"}
    assert "Auth" not in project["Source"]
    assert "python3 -I -S -" in bootstrap_script()
    role = document["Resources"]["CodeBuildRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert role[-3] == {"Effect": "Allow", "Action": "s3:GetObject",
                        "Resource": {"Sub": "arn:${AWS::Partition}:s3:::${SourceArchiveBucket}/${SourceArchiveKey}"}}
    assert role[-2] == {"Effect": "Allow", "Action": "s3:GetObjectVersion",
                        "Resource": {"Sub": "arn:${AWS::Partition}:s3:::${SourceArchiveBucket}/${SourceArchiveKey}"},
                        "Condition": {"StringEquals": {"s3:VersionId": {"Ref": "SourceArchiveVersionId"}}}}
    assert role[-1] == {"Effect": "Allow", "Action": ["s3:GetBucketAcl", "s3:GetBucketLocation"],
                        "Resource": {"Sub": "arn:${AWS::Partition}:s3:::${SourceArchiveBucket}"}}
    assert "secretsmanager" not in str(role)
    assert "codeconnections" not in str(role)
    assert "SECRETS_MANAGER" not in str(project["Environment"])
    trigger = document["Resources"]["ImageBuildTrigger"]["Properties"]
    for name in ["SourceArchiveBucket", "SourceArchiveKey", "SourceArchiveVersionId", "SourceVersion", "SourceManifestSha256"]:
        assert trigger[name] == {"Ref": name}


def test_artifact_bucket_is_private_encrypted_versioned_and_retained():
    document = yaml.load((ROOT / "infra/artifacts.yaml").read_text(), Loader=CloudFormationLoader)
    bucket = document["Resources"]["SourceBucket"]
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    assert bucket["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"
    assert all(bucket["Properties"]["PublicAccessBlockConfiguration"].values())
    assert bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
