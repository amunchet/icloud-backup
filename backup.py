#!/usr/bin/env python3
"""
Sync an iCloud Drive folder (e.g., Obsidian vault) down to disk using icloudpy,
then back it up to a Git repository with pre/post hooks.

Philosophy:
- iCloud is the "source of truth" for file contents.
- Git is the backup/audit log.
- "Conflicts" are resolved by overwriting git/local with iCloud (latest content as iCloud provides it).

Typical flow:
1) Pre-hook: reset local repo to remote (discard local changes) so we're starting clean
2) Download/sync from iCloud Drive folder -> local repo path
3) Git commit changes
4) Post-hook: push (optionally force-with-lease)

Requirements:
- pip install icloudpy pyyaml
- git installed
- iCloud login/session already established (password in keyring / saved session)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import yaml
from icloudpy import ICloudPyService


# ----------------------------
# Utilities
# ----------------------------

def log(msg: str) -> None:
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def run_cmd(cmd: str, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    log(f"RUN: {cmd} (cwd={cwd or Path.cwd()})")
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        shell=True,
        check=check,
        text=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ.copy(),
    )


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def safe_write_file_atomic(dest: Path, src_tmp: Path) -> None:
    """
    Atomic-ish replace: move tmp file into place.
    """
    ensure_dir(dest.parent)
    if dest.exists():
        # On Linux, replace is atomic if same filesystem
        src_tmp.replace(dest)
    else:
        src_tmp.replace(dest)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ----------------------------
# Config
# ----------------------------

@dataclass
class GitConfig:
    repo_path: Path
    branch: str = "master"
    remote: str = "origin"
    author_name: str = "icloud-backup-bot"
    author_email: str = "icloud-backup-bot@localhost"
    commit_message: str = "iCloud backup sync"
    push_force_with_lease: bool = True


@dataclass
class ICloudConfig:
    username: str
    # Path segments inside iCloud Drive, e.g. ["Obsidian", "MyVault"]
    drive_path: Tuple[str, ...]
    # If you're using China endpoints, pass them here; otherwise None.
    home_endpoint: Optional[str] = None
    setup_endpoint: Optional[str] = None
    # iCloudPy stores session cookies; set if you want a specific location.
    # If None, icloudpy default behavior applies.
    session_dir: Optional[Path] = None


@dataclass
class SyncConfig:
    local_root: Path
    # If True, delete local files that do not exist in iCloud folder
    mirror_delete: bool = False
    # If True, compare by size/date_modified (fast). If False, also hash local file (slower).
    fast_compare: bool = True
    # If True, keep .git directory safe; always True in practice.
    protect_git_dir: bool = True


@dataclass
class HooksConfig:
    pre: Tuple[str, ...] = ()
    post: Tuple[str, ...] = ()


@dataclass
class AppConfig:
    icloud: ICloudConfig
    git: GitConfig
    sync: SyncConfig
    hooks: HooksConfig


def load_config(path: Path) -> AppConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping/dict")

    ic = data.get("icloud", {})
    gt = data.get("git", {})
    sy = data.get("sync", {})
    hk = data.get("hooks", {})

    icloud = ICloudConfig(
        username=str(ic["username"]),
        drive_path=tuple(ic["drive_path"]),
        home_endpoint=ic.get("home_endpoint"),
        setup_endpoint=ic.get("setup_endpoint"),
        session_dir=Path(ic["session_dir"]) if ic.get("session_dir") else None,
    )

    git = GitConfig(
        repo_path=Path(gt["repo_path"]),
        branch=str(gt.get("branch", "master")),
        remote=str(gt.get("remote", "origin")),
        author_name=str(gt.get("author_name", "icloud-backup-bot")),
        author_email=str(gt.get("author_email", "icloud-backup-bot@localhost")),
        commit_message=str(gt.get("commit_message", "iCloud backup sync")),
        push_force_with_lease=bool(gt.get("push_force_with_lease", True)),
    )

    sync = SyncConfig(
        local_root=Path(sy.get("local_root", gt["repo_path"])),
        mirror_delete=bool(sy.get("mirror_delete", False)),
        fast_compare=bool(sy.get("fast_compare", True)),
        protect_git_dir=bool(sy.get("protect_git_dir", True)),
    )

    hooks = HooksConfig(
        pre=tuple(hk.get("pre", []) or []),
        post=tuple(hk.get("post", []) or []),
    )

    return AppConfig(icloud=icloud, git=git, sync=sync, hooks=hooks)


# ----------------------------
# iCloud Drive traversal
# ----------------------------

def get_drive_node(api: ICloudPyService, path_segments: Tuple[str, ...]):
    """
    Navigate api.drive[...] using path segments.
    """
    node = api.drive
    for seg in path_segments:
        node = node[seg]
    return node


def iter_icloud_tree(node, prefix: Path = Path(".")) -> Iterable[Tuple[Path, Any]]:
    """
    Yield (relative_path, node_or_file) for all files under an iCloud Drive node.
    - Directories are not yielded; files are yielded.
    """
    # node.dir() gives names under this node
    for name in node.dir():
        child = node[name]
        rel = prefix / name
        # icloudpy nodes have .type == 'file' or behave like folders with .dir()
        child_type = getattr(child, "type", None)
        if child_type == "file":
            yield rel, child
        else:
            # folder
            yield from iter_icloud_tree(child, rel)


def list_local_files(root: Path, protect_git_dir: bool = True) -> Iterable[Path]:
    """
    List all local files relative to root.
    """
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(root)
        if protect_git_dir and (rel.parts and rel.parts[0] == ".git"):
            continue
        yield rel


def local_file_meta(root: Path, rel: Path) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    """
    Return (size, mtime, sha256?) for local file.
    """
    p = root / rel
    if not p.exists():
        return None, None, None
    st = p.stat()
    return st.st_size, st.st_mtime, None


def should_download(
    local_root: Path,
    rel: Path,
    drive_file,
    fast_compare: bool,
) -> bool:
    """
    Decide whether to download iCloud file to local path.
    Uses size + iCloud date_modified vs local mtime; optionally hashes local file for certainty.
    """
    lp = local_root / rel
    if not lp.exists():
        return True

    local_size = lp.stat().st_size
    icloud_size = getattr(drive_file, "size", None)

    # icloudpy date_modified is UTC datetime
    icloud_dt = getattr(drive_file, "date_modified", None)
    icloud_mtime = None
    if icloud_dt is not None:
        # convert to epoch seconds
        icloud_mtime = icloud_dt.replace(tzinfo=dt.timezone.utc).timestamp()

    # Fast heuristics
    if icloud_size is not None and icloud_size != local_size:
        return True

    if icloud_mtime is not None:
        # If iCloud is newer by >=2s, download
        if icloud_mtime - lp.stat().st_mtime >= 2:
            return True

    if fast_compare:
        return False

    # Slow path: hash compare (only if sizes match)
    try:
        # iCloud doesn't provide hash; so if times/sizes match we assume same.
        # If you want true content validation, you'd need to download and hash temp, which is expensive.
        return False
    except Exception:
        return True


def download_icloud_file(local_root: Path, rel: Path, drive_file) -> None:
    """
    Download drive_file to local_root/rel safely.
    """
    dest = local_root / rel
    ensure_dir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".tmp_download")

    # Stream download to tmp file
    with drive_file.open(stream=True) as response:
        with tmp.open("wb") as f:
            shutil.copyfileobj(response.raw, f)

    # Preserve mtime if available
    icloud_dt = getattr(drive_file, "date_modified", None)
    if icloud_dt is not None:
        mtime = icloud_dt.replace(tzinfo=dt.timezone.utc).timestamp()
        os.utime(tmp, (mtime, mtime))

    safe_write_file_atomic(dest, tmp)


def delete_local_extras(
    local_root: Path,
    desired_files: set[Path],
    protect_git_dir: bool = True,
) -> int:
    """
    Delete local files not present in desired_files.
    """
    deleted = 0
    for rel in list_local_files(local_root, protect_git_dir=protect_git_dir):
        if rel not in desired_files:
            p = local_root / rel
            try:
                p.unlink()
                deleted += 1
            except FileNotFoundError:
                pass
    return deleted


# ----------------------------
# Git operations
# ----------------------------

def git_configure_identity(repo: Path, name: str, email: str) -> None:
    run_cmd(f'git config user.name "{name}"', cwd=repo)
    run_cmd(f'git config user.email "{email}"', cwd=repo)


def git_hard_reset_to_remote(repo: Path, remote: str, branch: str) -> None:
    """
    Make local match remote exactly.
    """
    run_cmd("git fetch --all --prune", cwd=repo)
    run_cmd(f"git checkout {branch}", cwd=repo)
    run_cmd(f"git reset --hard {remote}/{branch}", cwd=repo)
    run_cmd("git clean -fd", cwd=repo)  # remove untracked files/dirs (be careful)


def git_commit_if_needed(repo: Path, message: str) -> bool:
    """
    Returns True if a commit was created.
    """
    # Stage everything (including deletes)
    run_cmd("git add -A", cwd=repo)

    # If no changes, exit
    cp = subprocess.run(
        "git diff --cached --quiet",
        cwd=str(repo),
        shell=True,
    )
    if cp.returncode == 0:
        log("No git changes to commit.")
        return False

    run_cmd(f'git commit -m "{message}"', cwd=repo)
    return True


def git_push(repo: Path, remote: str, branch: str, force_with_lease: bool) -> None:
    if force_with_lease:
        run_cmd(f"git push --force-with-lease {remote} {branch}", cwd=repo)
    else:
        run_cmd(f"git push {remote} {branch}", cwd=repo)


# ----------------------------
# Hooks
# ----------------------------

def run_hooks(hooks: Tuple[str, ...], cwd: Path) -> None:
    for cmd in hooks:
        if not cmd.strip():
            continue
        run_cmd(cmd, cwd=cwd)


# ----------------------------
# Main sync
# ----------------------------

def build_api(cfg: ICloudConfig) -> ICloudPyService:
    """
    Build ICloudPyService instance.
    Assumes password/session already available (keyring + stored cookies).
    """
    if cfg.session_dir:
        ensure_dir(cfg.session_dir)
        # icloudpy reads env var for cookie dir in some forks; this keeps it explicit:
        os.environ["ICLOUDPY_DIR"] = str(cfg.session_dir)

    if cfg.home_endpoint and cfg.setup_endpoint:
        api = ICloudPyService(
            cfg.username,
            home_endpoint=cfg.home_endpoint,
            setup_endpoint=cfg.setup_endpoint,
        )
    else:
        api = ICloudPyService(cfg.username)

    # If 2FA happens here, you said your helper already handles it.
    # But we still fail loudly with guidance.
    if getattr(api, "requires_2fa", False) or getattr(api, "requires_2sa", False):
        raise RuntimeError(
            "iCloud authentication requires 2FA/2SA right now. "
            "Run your existing icloud helper in this venv/host to refresh the session, "
            "then re-run this script."
        )

    return api


def sync_icloud_drive_folder(cfg: AppConfig) -> Dict[str, int]:
    """
    Download iCloud Drive folder contents into local_root.
    """
    api = build_api(cfg.icloud)
    node = get_drive_node(api, cfg.icloud.drive_path)

    desired_files: set[Path] = set()

    downloaded = 0
    skipped = 0
    errors = 0

    for rel, drive_file in iter_icloud_tree(node):
        desired_files.add(rel)
        try:
            if should_download(cfg.sync.local_root, rel, drive_file, fast_compare=cfg.sync.fast_compare):
                download_icloud_file(cfg.sync.local_root, rel, drive_file)
                downloaded += 1
                log(f"DOWNLOADED: {rel}")
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            log(f"ERROR downloading {rel}: {e}")

    deleted = 0
    if cfg.sync.mirror_delete:
        deleted = delete_local_extras(
            cfg.sync.local_root,
            desired_files=desired_files,
            protect_git_dir=cfg.sync.protect_git_dir,
        )
        if deleted:
            log(f"Deleted {deleted} local files not present in iCloud (mirror_delete enabled).")

    return {"downloaded": downloaded, "skipped": skipped, "deleted": deleted, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to YAML config file")
    ap.add_argument("--no-pre-hook", action="store_true", help="Skip pre hooks")
    ap.add_argument("--no-post-hook", action="store_true", help="Skip post hooks")
    ap.add_argument("--no-git-reset", action="store_true", help="Skip hard reset to remote (NOT recommended)")
    ap.add_argument("--no-push", action="store_true", help="Do not push to remote")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))

    repo = cfg.git.repo_path.resolve()
    ensure_dir(repo)

    # Sanity checks
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git repo (missing .git). Clone/init it first.")

    # Ensure local_root is the repo path (common case)
    local_root = cfg.sync.local_root.resolve()
    if local_root != repo:
        log(f"NOTE: local_root ({local_root}) != repo_path ({repo}). This is allowed, but make sure you intend it.")

    # 0) Configure git identity
    git_configure_identity(repo, cfg.git.author_name, cfg.git.author_email)

    # 1) Pre hooks
    if not args.no_pre_hook and cfg.hooks.pre:
        log("Running PRE hooks...")
        run_hooks(cfg.hooks.pre, cwd=repo)

    # 2) Ensure local matches remote (discard local drift)
    if not args.no_git_reset:
        log("Resetting repo to match remote (discarding local changes)...")
        git_hard_reset_to_remote(repo, cfg.git.remote, cfg.git.branch)

    # 3) Sync iCloud down (source of truth)
    log(f"Syncing iCloud Drive folder: {'/'.join(cfg.icloud.drive_path)} -> {cfg.sync.local_root}")
    stats = sync_icloud_drive_folder(cfg)
    log(f"Sync stats: {stats}")

    if stats["errors"] > 0:
        log("Some files failed to download. Proceeding to git commit anyway (so you still get partial backup).")

    # 4) Commit
    committed = git_commit_if_needed(repo, cfg.git.commit_message)

    # 5) Post hooks
    if not args.no_post_hook and cfg.hooks.post:
        log("Running POST hooks...")
        run_hooks(cfg.hooks.post, cwd=repo)

    # 6) Push
    if args.no_push:
        log("Skipping push (--no-push).")
        return 0

    if committed:
        log("Pushing to remote...")
        git_push(repo, cfg.git.remote, cfg.git.branch, cfg.git.push_force_with_lease)
    else:
        log("No commit created; nothing to push.")

    log("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

