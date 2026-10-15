import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import quota_server
from git_worktrees import (
    ISOLATION_SHARED,
    ISOLATION_WORKTREE,
    find_git,
    probe_git_workspace,
    run_git,
)
import proxy_policy
from worktree_coordinator import WorktreeCoordinator

HANDOFF_MARKER = "ASTRA_IMPLEMENTATION_RESULT"
HANDOFF_FIELDS = ("summary", "files_changed", "tests_run", "tests_passed", "remaining_work", "risks", "commit_sha", "worktree", "quota_impact")
CACHE_TTL = 30.0
FILE_RE = re.compile(r"(?<![\w.-])([\w@+~.-]+(?:[/\\][\w@+~.-]+)+\.[A-Za-z0-9]{1,12})")
TEST_RE = re.compile(r"(?i)(?:python\s+-m\s+)?(?:pytest|unittest)|npm\s+test|pnpm\s+test|yarn\s+test|cargo\s+test|go\s+test|dotnet\s+test|mvn\s+test|gradle\s+test")

HANDOFF_INSTRUCTIONS = f'''Implementation handoff contract (required):
At the end of every implementation turn, emit `{HANDOFF_MARKER}` followed by exactly one JSON object with keys:
summary, files_changed, tests_run, tests_passed, remaining_work, risks, commit_sha, worktree, quota_impact.
Use arrays for files_changed/tests_run/remaining_work/risks; tests_passed may be null when no meaningful tests ran.
Git/worktree/quota fields are advisory only; Astra verifies them server-side.'''


def _list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value)]


def _with_contract(prompt, task_type="implementation", required_capabilities=None):
    if task_type != "implementation":
        from task_types import task_instructions
        return f"{prompt.strip()}\n\n{task_instructions(task_type, required_capabilities)}"
    prompt = prompt.strip()
    return prompt if HANDOFF_MARKER in prompt else f"{prompt}\n\n{HANDOFF_INSTRUCTIONS}"


def _parse_handoff(text):
    if not isinstance(text, str) or not text.strip():
        return None, "Gemini response was empty; no structured handoff was available."
    starts = []
    marker = text.rfind(HANDOFF_MARKER)
    if marker >= 0:
        pos = text.find("{", marker)
        if pos >= 0:
            starts.append(pos)
    starts += [m.start() for m in re.finditer(r"\{", text)][::-1]
    decoder = json.JSONDecoder()
    for pos in starts:
        try:
            obj, _ = decoder.raw_decode(text[pos:])
        except Exception:
            continue
        if isinstance(obj, dict):
            obj = obj.get("implementation") if isinstance(obj.get("implementation"), dict) else obj
            if any(k in obj for k in HANDOFF_FIELDS):
                missing = [k for k in HANDOFF_FIELDS if k not in obj]
                return obj, ("Gemini structured handoff was incomplete; missing: " + ", ".join(missing)) if missing else None
    return None, "Gemini did not emit a parseable structured implementation handoff."


def _texts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out = []
        for v in value.values():
            out += _texts(v)
        return out
    if isinstance(value, list):
        out = []
        for v in value:
            out += _texts(v)
        return out
    return []


class AdvancedWorktreeCoordinator(WorktreeCoordinator):
    def __init__(self, quota_coordinator=None, store=None):
        super().__init__(quota_coordinator, store)
        self._preflight_cache = {}
        self._runtime_preflight_enabled = quota_coordinator is None or quota_coordinator is quota_server.coordinator

    @staticmethod
    def _check(name, state, message, **details):
        row = {"name": name, "state": state, "message": message}
        if details:
            row["details"] = details
        return row

    def _git_snapshot(self, workspace, base=None):
        path = Path(workspace).expanduser().resolve()
        try:
            root = run_git(["-C", str(path), "rev-parse", "--show-toplevel"], check=False)
        except (RuntimeError, OSError):
            root = None
        if root is None or root.returncode != 0:
            return {"repo_root": None, "base_commit": base, "head_commit": None, "branch": None, "changed_files": [], "diff_stat": None, "commits_ahead": None, "dirty": None}
        repo = Path(root.stdout.strip()).resolve()
        head_r = run_git(["-C", str(repo), "rev-parse", "HEAD"], check=False)
        head = head_r.stdout.strip() if head_r.returncode == 0 else None
        base = base or head
        status = run_git(["-C", str(repo), "status", "--porcelain=v1", "--untracked-files=normal"], check=False)
        status_text = status.stdout.rstrip() if status.returncode == 0 else ""
        branch = run_git(["-C", str(repo), "branch", "--show-current"], check=False)
        names = set()
        stat = None
        ahead = None
        if base:
            diff = run_git(["-C", str(repo), "diff", "--name-only", base], check=False)
            if diff.returncode == 0:
                names.update(x.strip() for x in diff.stdout.splitlines() if x.strip())
            untracked = run_git(["-C", str(repo), "ls-files", "--others", "--exclude-standard"], check=False)
            if untracked.returncode == 0:
                names.update(x.strip() for x in untracked.stdout.splitlines() if x.strip())
            sr = run_git(["-C", str(repo), "diff", "--shortstat", base], check=False)
            stat = sr.stdout.strip() if sr.returncode == 0 else None
            ar = run_git(["-C", str(repo), "rev-list", "--count", f"{base}..HEAD"], check=False)
            if ar.returncode == 0:
                try: ahead = int(ar.stdout.strip())
                except ValueError: pass
        return {"repo_root": str(repo), "base_commit": base, "head_commit": head, "branch": (branch.stdout.strip() or None) if branch.returncode == 0 else None, "changed_files": sorted(names), "diff_stat": stat, "commits_ahead": ahead, "dirty": bool(status_text)}

    def _core_health(self, workspace=None, use_cache=True):
        key = str(Path(workspace).expanduser().resolve()).lower() if workspace else "<none>"
        now = time.time()
        cached = self._preflight_cache.get(key)
        if use_cache and cached and now - cached[0] <= CACHE_TTL:
            return {**cached[1], "cached": True}
        checks = []
        agy = git = None
        try:
            agy = quota_server.server.find_agy()
            ver = subprocess.run([agy, "--version"], capture_output=True, text=True, timeout=8)
            checks.append(self._check("agy", "ok" if ver.returncode == 0 else "error", "Antigravity CLI discovered." if ver.returncode == 0 else "Antigravity --version failed.", path=agy, version=(ver.stdout or ver.stderr or "").strip()[:500]))
        except Exception as exc:
            checks.append(self._check("agy", "error", f"Antigravity CLI is unavailable: {exc}"))
        if agy:
            try:
                help_r = subprocess.run([agy, "--help"], capture_output=True, text=True, timeout=8)
                help_text = (help_r.stdout or "") + (help_r.stderr or "")
                state = "ok" if "--input-format" in help_text else "error"
                checks.append(self._check("streaming_input", state, "Streaming input is supported." if state == "ok" else "Antigravity help does not advertise --input-format."))
            except Exception as exc:
                checks.append(self._check("streaming_input", "error", f"Could not verify streaming input: {exc}"))
        try:
            git = find_git(); checks.append(self._check("git", "ok", "Git discovered.", path=git))
        except Exception as exc:
            checks.append(self._check("git", "warning", f"Git unavailable; worktree isolation is disabled: {exc}"))
        try:
            state_dir = Path(self.base.state_dir).resolve(); state_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=state_dir): pass
            checks.append(self._check("state_directory", "ok", "State directory is writable.", path=str(state_dir)))
        except Exception as exc:
            checks.append(self._check("state_directory", "error", f"State directory is not writable: {exc}"))
        invalid = [n for n in ("AGY_PATH", "UV_PATH", "ASTRA_PYTHON_PATH", "ASTRA_ANTIGRAVITY_GIT_PATH") if os.environ.get(n) and not Path(os.environ[n]).expanduser().is_file()]
        checks.append(self._check("environment", "warning" if invalid else "ok", "Environment overrides loaded.", invalid_overrides=invalid))
        ws = {"path": None, "exists": None, "git_repository": None, "worktree_eligible": None, "dirty": None}
        if workspace:
            p = Path(workspace).expanduser().resolve(); ws.update(path=str(p), exists=p.is_dir())
            if not p.is_dir():
                checks.append(self._check("workspace", "error", f"Workspace does not exist: {p}"))
            elif git:
                allow_dirty = bool(getattr(self.store, "allow_dirty_source", False))
                probe = probe_git_workspace(p, allow_dirty=allow_dirty)
                ws.update(
                    git_repository=probe["git_repository"],
                    worktree_eligible=probe["worktree_eligible"],
                    dirty=probe["dirty"],
                    repo_root=probe.get("repo_root"),
                )
                if probe.get("timed_out"):
                    ws["timed_out"] = True
                    ws["error"] = probe["error"]
                    checks.append(self._check("workspace", "error", f"Git health probe timed out: {probe['error']}", **ws))
                elif not probe["git_repository"]:
                    if probe.get("error"):
                        checks.append(self._check("workspace", "warning", f"Git inspection failed: {probe['error']}", **ws))
                    else:
                        checks.append(self._check("workspace", "warning", "Workspace is not a Git repository; worktree isolation is disabled.", **ws))
                elif probe["worktree_eligible"]:
                    checks.append(self._check("workspace", "ok", "Workspace is eligible for isolated worktrees.", **ws))
                else:
                    checks.append(self._check("workspace", "warning", "Source repository is dirty; worktree isolation is disabled unless dirty source is allowed.", **ws))
        else:
            checks.append(self._check("workspace", "warning", "No workspace supplied; repository eligibility was not checked."))
        result = {"checked_at": now, "ready": not any(c["state"] == "error" for c in checks), "cached": False, "checks": checks, "agy_path": agy, "git_path": git, "workspace": ws}
        self._preflight_cache[key] = (now, result)
        return result

    def check_health(self, workspace=None):
        core = self._core_health(workspace)
        checks = list(core["checks"])
        try:
            quota = self.quota.get_quota_snapshot(workspace)
        except Exception as exc:
            quota = {"available": False, "blocked": False, "error": str(exc)}
        error = str(quota.get("error") or "")
        auth_bad = bool(re.search(r"(?i)auth|login|credential|unauthor|forbidden", error))
        checks.append(self._check("authentication", "error" if auth_bad else ("ok" if quota.get("available") else "warning"), "Authentication verified through /usage." if quota.get("available") else "Authentication could not be conclusively verified.", error=error or None))
        checks.append(self._check("usage_support", "ok" if quota.get("available") else "warning", "Structured /usage quota data is available." if quota.get("available") else "Structured /usage data is unavailable; quota remains fail-open.", error=error or None))
        checks.append(self._check("quota", "error" if quota.get("blocked") else ("warning" if quota.get("warning") or not quota.get("available") else "ok"), "Gemini quota state checked.", snapshot=quota))
        return {**core, "checked_at": time.time(), "checks": checks, "quota": quota, "ready": not any(c["state"] == "error" for c in checks)}

    def recommend_delegation(self, task, workspace):
        if not task.strip(): raise ValueError("Task must not be empty.")
        h = self.check_health(workspace); q = h.get("quota") or {}; low = task.lower()
        impl = any(x in low for x in ("implement", "build", "add ", "create", "fix", "refactor", "migrate", "update", "feature", "bug", "code", "api", "database"))
        score = int(len(task) >= 300) + int(len(task) >= 1000) + int(task.count("\n") >= 6)
        complexity = "large" if score >= 2 else ("medium" if score else "small")
        isolation = ISOLATION_WORKTREE if (h.get("workspace") or {}).get("worktree_eligible") else ISOLATION_SHARED
        try:
            active_tasks = sum(1 for r in getattr(self.base, "_tasks", {}).values() if getattr(r, "status", None) in ("started", "running"))
        except Exception:
            active_tasks = 0
        reasons, warnings = [], []
        if not h.get("ready") or q.get("blocked"):
            rec = "keep_in_codex"; reasons.append("Preflight or quota has a blocking condition.")
        elif not impl or (complexity == "small" and len(task) < 180):
            rec = "keep_in_codex"; reasons.append("Delegation overhead is unlikely to help for this task.")
        else:
            rec = "delegate"; reasons.append(f"Task is implementation-heavy with {complexity} estimated complexity.")
        if not q.get("available"): warnings.append("Quota is unavailable; delegation would use fail-open quota behavior.")
        elif q.get("warning"): warnings.append("Gemini quota is low.")
        if isolation == ISOLATION_SHARED and impl: warnings.append("Shared isolation reduces safe parallelism.")
        if active_tasks and isolation == ISOLATION_WORKTREE:
            reasons.append(f"{active_tasks} Gemini task(s) are already active; isolated worktrees preserve parallel execution.")
        if rec == "delegate" and warnings: rec = "delegate_with_warning"
        return {
            "recommendation": rec,
            "reasons": reasons,
            "warnings": warnings,
            "suggested_isolation": isolation,
            "proxy_spec": {
                **proxy_policy.get_proxy_spec(),
                "fallback": "direct_mcp",
                "verifier": "parent_astra",
            },
            "task_assessment": {"implementation_heavy": impl, "complexity": complexity, "size_chars": len(task)},
            "parallelism": {"active_tasks": active_tasks, "isolated_parallelism_available": isolation == ISOLATION_WORKTREE},
            "quota": q,
            "health_ready": h.get("ready", False),
        }

    def delegate(self, task, workspace, isolation=ISOLATION_SHARED, quota_override=False, model=None,
                 task_type="implementation", required_capabilities=None):
        if self._runtime_preflight_enabled:
            core = self._core_health(workspace)
            blocking = [c for c in core["checks"] if c["state"] == "error"]
            if blocking:
                return {"status": "preflight_failed", "preflight": core, "message": "; ".join(c["message"] for c in blocking), "isolation": isolation}
        baseline = self._git_snapshot(workspace) if isolation == ISOLATION_SHARED else None
        result = super().delegate(_with_contract(task, task_type, required_capabilities), workspace, isolation,
                                  quota_override=quota_override, model=model, task_type=task_type,
                                  required_capabilities=required_capabilities)
        task_id = result.get("task_id")
        if task_id and isolation == ISOLATION_SHARED and baseline:
            meta = self.store.load(task_id) or {}
            meta.update({"source_repo_root": baseline["repo_root"], "base_commit": baseline["base_commit"], "head_commit": baseline["head_commit"], "branch": baseline["branch"], "source_dirty": baseline["dirty"]})
            self.store.save(task_id, meta)
        return result

    def steer(self, task_id, instruction, quota_override=False):
        record = self.base.get_task(task_id)
        return super().steer(task_id, _with_contract(instruction, getattr(record, "task_type", "implementation"),
                                                     getattr(record, "required_capabilities", None)),
                             quota_override=quota_override)

    def progress_summary(self, task_id):
        record = self.base.get_task(task_id)
        if not record: raise ValueError(f"Task '{task_id}' not found.")
        phase = "complete" if record.status == "completed" else record.status if record.status in ("failed", "cancelled", "unknown") else "working"
        activity = current_file = last_change = last_test = None; passed = None
        path = Path(record.log_path)
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try: event = json.loads(line)
                except Exception: continue
                if not isinstance(event, dict): continue
                for text in _texts(event):
                    clean = re.sub(r"\s+", " ", text).strip()
                    if clean: activity = clean[-240:]
                    files = FILE_RE.findall(text)
                    if files: current_file = files[-1].replace("\\", "/")
                    m = TEST_RE.search(text)
                    if m: last_test = clean[-240:]
                    low = text.lower()
                    if any(x in low for x in ("modified ", "updated ", "created ", "wrote ", "patched ", "changed ")): last_change = clean[-240:]
                    if last_test and any(x in low for x in ("failed", "failure", "error")): passed = False
                    elif last_test and any(x in low for x in ("passed", "success", " ok", "green")): passed = True
        return {"task_id": task_id, "status": record.status, "current_turn": record.current_turn_index, "phase": phase, "activity": activity, "current_file": current_file, "tests": {"last_command": last_test, "passed": passed}, "last_change": last_change, "updated_at": record.updated_at}

    def status(self, task_id):
        out = super().status(task_id); out["progress"] = self.progress_summary(task_id); return out

    def result(self, task_id, max_chars=quota_server.server.DEFAULT_MAX_RESULT_CHARS):
        raw = super().result(task_id, max_chars)
        report, parse_error = _parse_handoff(raw.get("response") or "")
        report = report or {}
        meta = self._metadata(task_id, True)
        if meta.get("isolation") == ISOLATION_SHARED and meta.get("effective_workspace"):
            snap = self._git_snapshot(meta["effective_workspace"], meta.get("base_commit"))
            meta.update({"source_repo_root": snap["repo_root"], "head_commit": snap["head_commit"], "branch": snap["branch"], "changed_files": snap["changed_files"], "diff_stat": snap["diff_stat"], "commits_ahead": snap["commits_ahead"], "dirty": snap["dirty"]})
            self.store.save(task_id, meta)
            raw = self._attach(task_id, raw, refresh=False)
        tests_passed = report.get("tests_passed") if report.get("tests_passed") in (True, False, None) else None
        risks = _list(report.get("risks"))
        if parse_error: risks.append(parse_error)
        if meta.get("isolation") == ISOLATION_SHARED and meta.get("source_dirty"): risks.append("Shared workspace was already dirty at delegation time; Git-derived changes may include pre-existing edits.")
        ahead = meta.get("commits_ahead")
        commit_sha = meta.get("head_commit") if isinstance(ahead, int) and ahead > 0 else None
        worktree = {"workspace": meta.get("effective_workspace"), "branch": meta.get("branch"), "base_commit": meta.get("base_commit"), "head_commit": meta.get("head_commit"), "dirty": meta.get("dirty"), "diff_stat": meta.get("diff_stat"), "commits_ahead": ahead}
        if meta.get("isolation") == ISOLATION_WORKTREE: worktree["path"] = meta.get("worktree_root")
        else: worktree.update(path=meta.get("source_repo_root"), isolation=ISOLATION_SHARED)
        summary = str(report.get("summary") or "").strip() or ((raw.get("response") or "").strip()[:500] or raw.get("error") or "No Gemini summary was available.")
        raw["implementation"] = {"summary": summary, "files_changed": _list(meta.get("changed_files")), "tests_run": _list(report.get("tests_run")), "tests_passed": tests_passed, "remaining_work": _list(report.get("remaining_work")), "risks": risks, "commit_sha": commit_sha, "worktree": worktree, "quota_impact": {"before": raw.get("quota_before"), "after": raw.get("quota_after"), "delta": raw.get("quota_delta"), "blocked_next_turn": raw.get("quota_blocked_next_turn", False), "blocked_reason": raw.get("quota_blocked_reason"), "quota_override_used": raw.get("quota_override_used", False)}, "structured_report_valid": parse_error is None, "reported": report or None}
        return raw
