import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

ISOLATION_SHARED = "shared"
ISOLATION_WORKTREE = "worktree"
SUPPORTED_ISOLATION_MODES = frozenset({ISOLATION_SHARED, ISOLATION_WORKTREE})
GIT_TIMEOUT_SECONDS = 30.0
GIT_HEALTH_TIMEOUT_SECONDS = 5.0
MAX_STATUS_CHARS = 12000
MAX_GIT_OUTPUT_BYTES = 1_000_000


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def find_git() -> str:
    override = os.environ.get("ASTRA_ANTIGRAVITY_GIT_PATH")
    if override and Path(override).is_file():
        return override
    found = shutil.which("git")
    if found:
        return found
    raise RuntimeError("Git is required for worktree isolation but was not found on PATH.")


def _kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def run_git_bounded(
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    check: bool = True,
    max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
) -> subprocess.CompletedProcess:
    git_bin = find_git()
    cmd = [git_bin, *args]

    with tempfile.TemporaryFile(mode="w+b") as out_tmp, tempfile.TemporaryFile(mode="w+b") as err_tmp:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=out_tmp,
                stderr=err_tmp,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to start Git command: {exc}") from exc

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc.pid)
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            raise subprocess.TimeoutExpired(cmd, timeout) from exc

        out_tmp.seek(0)
        err_tmp.seek(0)
        stdout = out_tmp.read(max_output_bytes).decode("utf-8", errors="replace")
        stderr = err_tmp.read(max_output_bytes).decode("utf-8", errors="replace")

        completed = subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                f"Git command failed ({completed.returncode}): {' '.join(args)}"
                + (f"\n{detail[:4000]}" if detail else "")
            )
        return completed


def run_git(
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return run_git_bounded(args, timeout=timeout, check=check)


def probe_git_workspace(
    workspace: str | Path,
    *,
    timeout: float = GIT_HEALTH_TIMEOUT_SECONDS,
    allow_dirty: bool = False,
) -> dict:
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        return {
            "git_repository": False,
            "repo_root": None,
            "worktree_eligible": False,
            "dirty": None,
            "error": f"Workspace does not exist: {workspace_path}",
            "timed_out": False,
        }

    try:
        root_res = run_git(
            ["-C", str(workspace_path), "rev-parse", "--show-toplevel"],
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "git_repository": False,
            "repo_root": None,
            "worktree_eligible": False,
            "dirty": None,
            "error": f"Git rev-parse timed out after {timeout}s",
            "timed_out": True,
        }
    except Exception as exc:
        return {
            "git_repository": False,
            "repo_root": None,
            "worktree_eligible": False,
            "dirty": None,
            "error": str(exc),
            "timed_out": False,
        }

    if root_res.returncode != 0:
        return {
            "git_repository": False,
            "repo_root": None,
            "worktree_eligible": False,
            "dirty": None,
            "error": None,
            "timed_out": False,
        }

    repo_root = Path(root_res.stdout.strip()).resolve()

    try:
        status_res = run_git(
            ["-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=normal"],
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "git_repository": True,
            "repo_root": str(repo_root),
            "worktree_eligible": False,
            "dirty": None,
            "error": f"Git status timed out after {timeout}s",
            "timed_out": True,
        }
    except Exception as exc:
        return {
            "git_repository": True,
            "repo_root": str(repo_root),
            "worktree_eligible": False,
            "dirty": None,
            "error": str(exc),
            "timed_out": False,
        }

    if status_res.returncode != 0:
        return {
            "git_repository": True,
            "repo_root": str(repo_root),
            "worktree_eligible": False,
            "dirty": None,
            "error": f"Git status failed ({status_res.returncode})",
            "timed_out": False,
        }

    status_text = status_res.stdout.rstrip()
    dirty = bool(status_text)
    eligible = not dirty or allow_dirty

    return {
        "git_repository": True,
        "repo_root": str(repo_root),
        "worktree_eligible": eligible,
        "dirty": dirty,
        "error": None,
        "timed_out": False,
    }


def _changed_files(root: Path, base_commit: str) -> list[str]:
    names: set[str] = set()
    commands = (
        ["-C", str(root), "diff", "--name-only", base_commit],
        ["-C", str(root), "ls-files", "--others", "--exclude-standard"],
    )
    for command in commands:
        result = run_git(command, check=False)
        if result.returncode == 0:
            names.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(names)


class GitWorktreeStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self.allow_dirty_source = _env_bool(
            "ASTRA_ANTIGRAVITY_WORKTREE_ALLOW_DIRTY_SOURCE", False
        )

    @property
    def root(self) -> Path:
        configured = os.environ.get("ASTRA_ANTIGRAVITY_WORKTREE_ROOT")
        root = (
            Path(configured).expanduser().resolve()
            if configured
            else self.state_dir / "worktrees"
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _path(self, task_id: str) -> Path:
        return self.state_dir / f"{task_id}.worktree.json"

    def load(self, task_id: str) -> dict | None:
        with self._lock:
            if task_id in self._cache:
                return self._cache[task_id]
            path = self._path(task_id)
            if not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
            if not isinstance(value, dict):
                return None
            self._cache[task_id] = value
            return value

    def save(self, task_id: str, metadata: dict) -> dict:
        with self._lock:
            self._cache[task_id] = metadata
            target = self._path(task_id)
            tmp = target.with_suffix(f".tmp-{uuid.uuid4().hex[:6]}")
            try:
                tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
                tmp.replace(target)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return metadata

    def shared(self, task_id: str, source_workspace: str, effective_workspace: str) -> dict:
        metadata = {
            "task_id": task_id,
            "isolation": ISOLATION_SHARED,
            "source_workspace": str(Path(source_workspace).expanduser().resolve()),
            "effective_workspace": str(Path(effective_workspace).expanduser().resolve()),
            "worktree_root": None,
            "worktree_workspace": None,
            "worktree_exists": False,
            "branch": None,
            "base_commit": None,
            "head_commit": None,
            "changed_files": [],
            "diff_stat": None,
            "commits_ahead": None,
            "dirty": None,
            "created_at": time.time(),
            "cleaned_at": None,
        }
        return self.save(task_id, metadata)

    def _source_context(self, workspace: str) -> dict:
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace_path}")
        root_result = run_git(
            ["-C", str(workspace_path), "rev-parse", "--show-toplevel"], check=False
        )
        if root_result.returncode != 0:
            raise ValueError(f"Worktree isolation requires a Git workspace: {workspace_path}")
        repo_root = Path(root_result.stdout.strip()).resolve()
        try:
            relative = workspace_path.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"Workspace '{workspace_path}' is outside repository '{repo_root}'.") from exc
        base_commit = run_git(["-C", str(repo_root), "rev-parse", "HEAD"]).stdout.strip()
        status = run_git(
            ["-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=normal"]
        ).stdout.rstrip()
        dirty = bool(status)
        if dirty and not self.allow_dirty_source:
            raise ValueError(
                "Worktree isolation requires a clean source repository so Gemini sees the "
                "same committed state Astra is delegating from. Commit/stash the source "
                "changes, use isolation='shared', or explicitly set "
                "ASTRA_ANTIGRAVITY_WORKTREE_ALLOW_DIRTY_SOURCE=1 to base the isolated "
                "worktree on HEAD while excluding source working-tree changes."
            )
        return {
            "source_workspace": str(workspace_path),
            "source_repo_root": str(repo_root),
            "relative_workspace": str(relative),
            "base_commit": base_commit,
            "source_dirty": dirty,
            "source_status": status[:MAX_STATUS_CHARS],
        }

    def create(self, workspace: str) -> dict:
        context = self._source_context(workspace)
        token = uuid.uuid4().hex[:12]
        branch = f"astra/gemini/wt-{token}"
        worktree_root = self.root / token
        repo_root = Path(context["source_repo_root"])
        metadata = {
            "isolation": ISOLATION_WORKTREE,
            **context,
            "worktree_root": str(worktree_root),
            "branch": branch,
        }
        try:
            run_git([
                "-C", str(repo_root), "worktree", "add", "-b", branch,
                str(worktree_root), context["base_commit"],
            ])
            effective = (worktree_root / Path(context["relative_workspace"])).resolve()
            if not effective.is_dir():
                raise RuntimeError(f"Isolated workspace was not created: {effective}")
        except Exception:
            self.discard(metadata)
            raise
        metadata.update({
            "worktree_root": str(worktree_root.resolve()),
            "worktree_workspace": str(effective),
            "effective_workspace": str(effective),
            "worktree_exists": True,
            "created_at": time.time(),
            "cleaned_at": None,
            "branch_deleted": False,
            "warning": (
                "Source repository was dirty; isolated worktree is based on HEAD and "
                "does not include source working-tree changes."
                if context["source_dirty"] else None
            ),
        })
        return metadata

    def discard(self, metadata: dict) -> None:
        repo = metadata.get("source_repo_root")
        root = metadata.get("worktree_root")
        branch = metadata.get("branch")
        if repo and root:
            run_git(["-C", str(repo), "worktree", "remove", "--force", str(root)], check=False)
            run_git(["-C", str(repo), "worktree", "prune"], check=False)
        if repo and branch:
            run_git(["-C", str(repo), "branch", "-D", str(branch)], check=False)
        if root:
            shutil.rmtree(root, ignore_errors=True)

    def register(self, task_id: str, metadata: dict) -> dict:
        metadata["task_id"] = task_id
        return self.save(task_id, metadata)

    def refresh(self, task_id: str, fallback_workspace: str | None = None) -> dict:
        metadata = self.load(task_id)
        if metadata is None:
            if fallback_workspace is None:
                raise ValueError(f"Task '{task_id}' has no worktree metadata.")
            return self.shared(task_id, fallback_workspace, fallback_workspace)
        if metadata.get("isolation") != ISOLATION_WORKTREE:
            return dict(metadata)
        root = Path(metadata["worktree_root"])
        metadata["worktree_exists"] = root.is_dir()
        if not root.is_dir():
            self.save(task_id, metadata)
            return dict(metadata)
        status = run_git(
            ["-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"],
            check=False,
        )
        status_text = status.stdout.rstrip() if status.returncode == 0 else ""
        metadata["status_porcelain"] = status_text[:MAX_STATUS_CHARS]
        metadata["dirty"] = bool(status_text)
        head = run_git(["-C", str(root), "rev-parse", "HEAD"], check=False)
        if head.returncode == 0:
            metadata["head_commit"] = head.stdout.strip()
        branch = run_git(["-C", str(root), "branch", "--show-current"], check=False)
        if branch.returncode == 0 and branch.stdout.strip():
            metadata["branch"] = branch.stdout.strip()
        base = metadata.get("base_commit")
        if base:
            metadata["changed_files"] = _changed_files(root, base)
            stat = run_git(["-C", str(root), "diff", "--shortstat", base], check=False)
            metadata["diff_stat"] = stat.stdout.strip() if stat.returncode == 0 else None
            ahead = run_git(["-C", str(root), "rev-list", "--count", f"{base}..HEAD"], check=False)
            try:
                metadata["commits_ahead"] = int(ahead.stdout.strip()) if ahead.returncode == 0 else None
            except ValueError:
                metadata["commits_ahead"] = None
        self.save(task_id, metadata)
        return dict(metadata)

    def remove(self, task_id: str, *, force: bool = False, delete_branch: bool = False) -> dict:
        metadata = self.refresh(task_id)
        if metadata.get("isolation") != ISOLATION_WORKTREE:
            return {"task_id": task_id, "cleaned": False, "message": "Task does not own an isolated worktree."}
        if metadata.get("dirty") and not force:
            return {
                "task_id": task_id,
                "cleaned": False,
                "message": "Worktree has uncommitted changes; review/integrate them first or use force=True to discard them.",
                "worktree": metadata,
            }
        repo = metadata["source_repo_root"]
        root = metadata["worktree_root"]
        branch = metadata.get("branch")
        removed = not Path(root).is_dir()
        remove_error = None
        if not removed:
            args = ["-C", repo, "worktree", "remove"]
            if force:
                args.append("--force")
            args.append(root)
            result = run_git(args, check=False)
            removed = result.returncode == 0
            if not removed:
                remove_error = (result.stderr or result.stdout or "").strip()[:4000]
            run_git(["-C", repo, "worktree", "prune"], check=False)
        branch_deleted = False
        branch_delete_error = None
        if removed and delete_branch and branch:
            result = run_git(
                ["-C", repo, "branch", "-D" if force else "-d", branch], check=False
            )
            branch_deleted = result.returncode == 0
            if not branch_deleted:
                branch_delete_error = (result.stderr or result.stdout or "").strip()[:4000]
        if removed:
            metadata.update({
                "worktree_exists": False,
                "dirty": False,
                "status_porcelain": "",
                "cleaned_at": time.time(),
                "branch_deleted": branch_deleted,
            })
            self.save(task_id, metadata)
        return {
            "task_id": task_id,
            "cleaned": removed,
            "branch_deleted": branch_deleted,
            "remove_error": remove_error,
            "branch_delete_error": branch_delete_error,
            "worktree": dict(metadata),
        }
