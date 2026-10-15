import json
import math
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

import server
from compatibility import call_supported

mcp = FastMCP("Antigravity")

DEFAULT_MODEL = server.DEFAULT_MODEL
DEFAULT_QUOTA_WARN_FRACTION = 0.15
DEFAULT_QUOTA_MIN_START_FRACTION = 0.05
QUOTA_SUBPROCESS_TIMEOUT_SECONDS = 35.0
QUOTA_PRINT_TIMEOUT = "30s"


def _env_fraction(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return max(0.0, min(value, 1.0))


def _normalize_bucket(value: dict) -> dict | None:
    remaining = value.get("remaining_fraction")
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
        return None
    out = {"remaining_fraction": max(0.0, min(float(remaining), 1.0))}
    if value.get("reset_time") is not None:
        out["reset_time"] = value["reset_time"]
    reset_in = value.get("reset_in_seconds")
    if isinstance(reset_in, (int, float)) and not isinstance(reset_in, bool):
        out["reset_in_seconds"] = max(0, int(reset_in))
    if isinstance(value.get("window"), str):
        out["window"] = value["window"]
    return out


UNRELATED_MODEL_KEYWORDS = frozenset({
    "claude", "gpt", "o1", "o3", "sonnet", "haiku", "opus", "codex"
})


def _extract_buckets(payload: Any) -> dict[str, dict]:
    """Extract quota buckets from Antigravity 1.1.11+ structured output.

    Preserves group and model attribution on extracted bucket entries.
    """
    found: dict[str, dict] = {}

    def walk(
        value: Any,
        path: tuple[str, ...] = (),
        current_group: str | None = None,
        current_model: str | None = None,
    ) -> None:
        if isinstance(value, dict):
            grp = (
                value.get("group_name")
                or value.get("name")
                or value.get("group")
                if "buckets" in value
                else current_group
            )
            mdl = value.get("model") or value.get("model_name") or current_model
            normalized = _normalize_bucket(value)
            if normalized:
                bucket_id = value.get("id") if isinstance(value.get("id"), str) else None
                key = bucket_id or (".".join(path) or "quota")
                entry = dict(normalized)
                if grp and isinstance(grp, str):
                    entry["group"] = grp
                if mdl and isinstance(mdl, str):
                    entry["model"] = mdl
                if value.get("name") and isinstance(value.get("name"), str):
                    entry["name"] = value.get("name")
                found[key] = entry
            for k, child in value.items():
                walk(
                    child,
                    path + (str(k),),
                    current_group=str(grp) if grp else current_group,
                    current_model=str(mdl) if mdl else current_model,
                )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(
                    child,
                    path + (str(index),),
                    current_group=current_group,
                    current_model=current_model,
                )
        elif isinstance(value, str):
            text = value.strip()
            if text[:1] in ("{", "["):
                try:
                    decoded = json.loads(text)
                except Exception:
                    decoded = None
                if decoded is not None:
                    walk(
                        decoded,
                        path + ("json",),
                        current_group=current_group,
                        current_model=current_model,
                    )

    walk(payload)
    if found:
        return found

    texts: list[str] = []
    if isinstance(payload, str):
        texts.append(payload)
    elif isinstance(payload, dict):
        for key in ("response", "report", "text", "output"):
            if isinstance(payload.get(key), str):
                texts.append(payload[key])

    pattern = re.compile(
        r"(?im)^\s*(?P<label>[A-Za-z0-9_. /()\-]{2,80}?)\s*[:\-]\s*"
        r"(?P<percent>\d+(?:\.\d+)?)\s*%\s*(?:remaining|left)\b"
    )
    for text in texts:
        for match in pattern.finditer(text):
            label = re.sub(r"\s+", "-", match.group("label").strip().lower())
            found[f"text.{label}"] = {
                "remaining_fraction": max(
                    0.0, min(float(match.group("percent")) / 100.0, 1.0)
                ),
                "name": label,
            }
    return found


def _relevant_buckets(buckets: dict[str, dict], model: str | None = None) -> dict[str, dict]:
    if not buckets:
        return {}

    target = (model or DEFAULT_MODEL).lower()
    family = re.sub(r"-(?:low|medium|high|xhigh|max)$", "", target)
    if target.startswith("claude-"):
        target_kind = "opus" if "opus" in target else "sonnet"
        target_version = re.search(r"(?:opus|sonnet)[- ](\d+(?:[-.]\d+)?)", target)
        version = target_version.group(1).replace("-", ".") if target_version else None
        relevant = {}
        for name, bucket in buckets.items():
            text = " ".join(str(value) for value in (name, bucket.get("name", ""),
                           bucket.get("group", ""), bucket.get("model", ""))).lower()
            if "gemini" in text:
                continue
            explicit = re.findall(r"(?:claude[- ])?(opus|sonnet)[- ](\d+(?:[-.]\d+)?)", text)
            if explicit:
                if any(kind == target_kind and number.replace("-", ".") == version for kind, number in explicit):
                    relevant[name] = bucket
                continue
            # Antigravity reports a shared Claude/GPT pool. A standalone GPT
            # bucket is unrelated unless it explicitly names that shared pool.
            group = str(bucket.get("group", "")).lower().strip()
            shared = group in ("claude", "claude models", "anthropic", "claude and gpt models",
                               "claude and gpt", "claude/gpt", "claude & gpt models")
            generic = re.fullmatch(r"claude(?:[-_. ]gpt(?:[-_. ]oss)?)?[-_. ](?:weekly|5h|five[-_. ]hour|quota|daily|monthly)(?:[-_. ].*)?", name.lower())
            if shared or generic or family in text:
                relevant[name] = bucket
        return relevant
    target_kind = "argon" if "argon" in family else "flash" if "flash" in family else "pro"
    relevant = {}
    for name, bucket in buckets.items():
        text = " ".join(str(v) for v in (name, bucket.get("name", ""),
                       bucket.get("group", ""), bucket.get("model", ""))).lower()
        if any(word in text for word in UNRELATED_MODEL_KEYWORDS):
            continue
        versions = re.findall(r"gemini[- ](\d+(?:\.\d+)?)[- ](argon|flash|pro)", text)
        if versions and not any(f"gemini-{version}-{kind}" == family
                                for version, kind in versions):
            continue
        kinds = {kind for kind in ("argon", "flash", "pro")
                 if re.search(rf"\b{kind}\b", text)}
        if kinds and target_kind not in kinds:
            continue
        if versions or kinds or family in text:
            relevant[name] = bucket
            continue
        group = str(bucket.get("group", "")).lower().strip()
        generic = re.fullmatch(r"gemini[-_. ](?:weekly|5h|five[-_. ]hour|quota|daily|monthly)(?:[-_. ].*)?", name.lower())
        if group in ("gemini", "gemini models", "shared gemini", "gemini shared") or generic:
            relevant[name] = bucket
    return relevant


def _quota_delta(before: dict | None, after: dict | None) -> dict | None:
    if not before or not after or not before.get("available") or not after.get("available"):
        return None
    b = before.get("buckets") or {}
    a = after.get("buckets") or {}
    deltas: dict[str, dict] = {}
    for name in sorted(set(b) & set(a)):
        before_fraction = b[name].get("remaining_fraction")
        after_fraction = a[name].get("remaining_fraction")
        if isinstance(before_fraction, (int, float)) and isinstance(after_fraction, (int, float)):
            deltas[name] = {
                "remaining_fraction_delta": after_fraction - before_fraction,
                "consumed_fraction": max(0.0, before_fraction - after_fraction),
            }
    return {"buckets": deltas} if deltas else None


class QuotaCoordinator:
    def __init__(self, base_manager=None):
        self.base = base_manager or server.manager
        self.warn_fraction = _env_fraction(
            "ASTRA_ANTIGRAVITY_QUOTA_WARN_FRACTION", DEFAULT_QUOTA_WARN_FRACTION
        )
        self.min_start_fraction = _env_fraction(
            "ASTRA_ANTIGRAVITY_QUOTA_MIN_START_FRACTION",
            DEFAULT_QUOTA_MIN_START_FRACTION,
        )
        self._lock = threading.RLock()
        self._quota_process_lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self._monitoring: set[tuple[str, int]] = set()
        self._advance_locks: dict[str, threading.Lock] = {}

    def _state_path(self, task_id: str) -> Path:
        return Path(self.base.state_dir) / f"{task_id}.quota.json"

    def _default_state(self) -> dict:
        return {
            "quota_before": None,
            "quota_after": None,
            "quota_delta": None,
            "quota_last_checked": None,
            "quota_blocked_next_turn": False,
            "quota_blocked_reason": None,
            "pending_instruction": None,
            "pending_error": None,
            "quota_checked_turn": None,
        }

    def _get_state(self, task_id: str) -> dict:
        with self._lock:
            if task_id in self._state:
                return self._state[task_id]
            state = self._default_state()
            path = self._state_path(task_id)
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        state.update(loaded)
                except Exception:
                    pass
            self._state[task_id] = state
            return state

    def _save_state(self, task_id: str) -> None:
        with self._lock:
            state = self._state.get(task_id)
            if state is None:
                return
            path = self._state_path(task_id)
            tmp = path.with_suffix(".quota.tmp")
            try:
                tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
                tmp.replace(path)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def _set_state(self, task_id: str, **updates) -> dict:
        with self._lock:
            state = self._get_state(task_id)
            state.update(updates)
            self._save_state(task_id)
            return dict(state)

    def get_quota_snapshot(
        self,
        workspace: str | None = None,
        model: str | None = None,
    ) -> dict:
        target_model = model or DEFAULT_MODEL
        snapshot = {
            "checked_at": time.time(),
            "model": target_model,
            "available": False,
            "buckets": {},
            "all_buckets": {},
            "min_remaining_fraction": None,
            "limiting_bucket": None,
            "warning": False,
            "blocked": False,
            "warn_fraction": self.warn_fraction,
            "min_start_fraction": self.min_start_fraction,
            "error": None,
            "source": "agy -p /usage",
        }

        cwd = None
        if workspace:
            try:
                candidate = Path(workspace).expanduser().resolve()
                if candidate.is_dir():
                    cwd = str(candidate)
            except Exception:
                pass

        try:
            command = [
                server.find_agy(),
                "-p",
                "/usage",
                "--output-format",
                "json",
                "--print-timeout",
                QUOTA_PRINT_TIMEOUT,
            ]
            with self._quota_process_lock:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=QUOTA_SUBPROCESS_TIMEOUT_SECONDS,
                )
        except Exception as exc:
            snapshot["error"] = f"Quota check failed: {exc}"
            return snapshot

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            snapshot["error"] = (
                f"Quota check exited with code {completed.returncode}: {detail[:1000]}"
            )
            return snapshot

        raw = (completed.stdout or "").strip()
        try:
            payload: Any = json.loads(raw)
        except Exception:
            payload = raw
        all_buckets = _extract_buckets(payload)
        relevant = _relevant_buckets(all_buckets, model=target_model)
        snapshot["all_buckets"] = all_buckets
        snapshot["buckets"] = relevant

        if not relevant:
            snapshot["error"] = (
                "Quota data was returned but no bucket could be matched safely to "
                f"{target_model}."
            )
            return snapshot

        limiting_name, limiting = min(
            relevant.items(), key=lambda item: item[1]["remaining_fraction"]
        )
        remaining = limiting["remaining_fraction"]
        snapshot.update(
            {
                "available": True,
                "min_remaining_fraction": remaining,
                "remaining_fraction": remaining,
                "limiting_bucket": limiting_name,
                "warning": remaining < self.warn_fraction,
                "blocked": remaining < self.min_start_fraction,
                "error": None,
            }
        )
        return snapshot

    @staticmethod
    def _blocked_message(stage: str, quota: dict) -> str:
        remaining = quota.get("min_remaining_fraction")
        pct = f"{remaining * 100:.1f}%" if isinstance(remaining, (int, float)) else "unknown"
        bucket = quota.get("limiting_bucket") or "unknown"
        return f"Gemini quota blocked {stage}: {bucket} has {pct} remaining."

    def _record_post_turn_quota(self, task_id: str, turn_index: int) -> dict:
        lock = self._advance_locks.setdefault(task_id, threading.Lock())
        with lock:
            record = self.base.get_task(task_id)
            if not record:
                raise ValueError(f"Task '{task_id}' not found.")
            state = self._get_state(task_id)
            if state.get("quota_checked_turn") == turn_index and state.get("quota_after"):
                return state["quota_after"]

            task_model = getattr(record, "model", None) or DEFAULT_MODEL
            quota = call_supported(self.get_quota_snapshot, record.workspace, model=task_model)
            updates = {
                "quota_after": quota,
                "quota_last_checked": quota,
                "quota_checked_turn": turn_index,
            }
            before = state.get("quota_before")
            updates["quota_delta"] = _quota_delta(before, quota)
            if quota.get("blocked"):
                updates["quota_blocked_next_turn"] = True
                updates["quota_blocked_reason"] = self._blocked_message(
                    "next turn", quota
                )
            else:
                updates["quota_blocked_next_turn"] = False
                updates["quota_blocked_reason"] = None
            self._set_state(task_id, **updates)
            return quota

    def _start_monitor(self, task_id: str, turn_index: int) -> None:
        key = (task_id, turn_index)
        with self._lock:
            if key in self._monitoring:
                return
            self._monitoring.add(key)

        def run() -> None:
            try:
                while True:
                    result = self.base.wait_for_task(task_id, 30)
                    if result.get("finished"):
                        break
                record = self.base.get_task(task_id)
                if not record or record.current_turn_index != turn_index:
                    return
                self._record_post_turn_quota(task_id, turn_index)
                self._advance_pending(task_id)
            except Exception as exc:
                self._set_state(task_id, pending_error=str(exc)[:2000])
            finally:
                with self._lock:
                    self._monitoring.discard(key)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

    def _advance_pending(self, task_id: str) -> bool:
        lock = self._advance_locks.setdefault(task_id, threading.Lock())
        with lock:
            record = self.base.get_task(task_id)
            if not record or record.status != "completed":
                return False
            state = self._get_state(task_id)
            instruction = state.get("pending_instruction")
            if not instruction:
                return False

            quota = state.get("quota_after")
            task_model = getattr(record, "model", None) or DEFAULT_MODEL
            if state.get("quota_checked_turn") != record.current_turn_index or not quota:
                quota = call_supported(self.get_quota_snapshot, record.workspace, model=task_model)
                self._set_state(
                    task_id,
                    quota_after=quota,
                    quota_last_checked=quota,
                    quota_checked_turn=record.current_turn_index,
                    quota_delta=_quota_delta(state.get("quota_before"), quota),
                )
                state = self._get_state(task_id)

            override_active = bool(state.get("quota_override_used"))
            if quota.get("blocked") and not override_active:
                self._set_state(
                    task_id,
                    pending_instruction=None,
                    quota_blocked_next_turn=True,
                    quota_blocked_reason=self._blocked_message("queued follow-up", quota),
                )
                return False

            self._set_state(
                task_id,
                pending_instruction=None,
                pending_error=None,
                quota_last_checked=quota,
                quota_blocked_next_turn=False,
                quota_blocked_reason=None,
            )
            try:
                result = self.base.steer_task(task_id, instruction)
            except Exception:
                self._set_state(task_id, pending_instruction=instruction)
                raise
            next_turn = int(result.get("turn_index") or record.current_turn_index + 1)
            self._start_monitor(task_id, next_turn)
            return True

    def delegate(
        self,
        task: str,
        workspace: str,
        quota_override: bool = False,
        model: str | None = None,
        task_type: str = "implementation",
        required_capabilities: list[str] | None = None,
    ) -> dict:
        target_model = model or DEFAULT_MODEL
        quota = call_supported(self.get_quota_snapshot, workspace, model=target_model)
        if quota.get("blocked") and not quota_override:
            return {
                "status": "quota_blocked",
                "quota_preflight": quota,
                "limiting_bucket": quota.get("limiting_bucket"),
                "remaining_fraction": quota.get("min_remaining_fraction"),
                "message": self._blocked_message("delegation", quota),
                "quota_override_used": False,
            }

        record = call_supported(self.base.start_task, task, workspace, model=target_model,
                                task_type=task_type, required_capabilities=required_capabilities)
        self._set_state(
            record.task_id,
            quota_before=quota,
            quota_last_checked=quota,
            quota_blocked_next_turn=False,
            quota_blocked_reason=None,
            quota_override_used=bool(quota_override),
        )
        self._start_monitor(record.task_id, record.current_turn_index)
        return {
            "task_id": record.task_id,
            "status": record.status,
            "pid": record.pid,
            "workspace": record.workspace,
            "log": record.log_path,
            "conversation_id": record.conversation_id,
            "session_alive": record.session_alive,
            "current_turn": record.current_turn_index,
            "quota_preflight": quota,
            "quota_before": quota,
            "quota_override_used": bool(quota_override),
        }

    def steer(self, task_id: str, instruction: str, quota_override: bool = False) -> dict:
        if not instruction.strip():
            raise ValueError("Instruction must not be empty.")
        record = self.base.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")

        state = self._get_state(task_id)
        override_active = bool(quota_override or state.get("quota_override_used"))
        if record.status in ("started", "running"):
            if state.get("pending_instruction"):
                raise ValueError("A quota-aware follow-up is already queued.")
            self._set_state(
                task_id,
                pending_instruction=instruction,
                pending_error=None,
                quota_override_used=override_active,
            )
            self._start_monitor(task_id, record.current_turn_index)
            return {
                "task_id": task_id,
                "status": "queued",
                "queued": True,
                "live_interruption": False,
                "turn_index": record.current_turn_index + 1,
                "conversation_id": record.conversation_id,
                "quota_override_used": override_active,
                "message": (
                    "Follow-up queued in the quota coordinator. It will only start "
                    "after the current result and a fresh quota check."
                ),
            }

        if record.status != "completed":
            raise ValueError(f"Cannot steer task '{task_id}' in status '{record.status}'.")

        task_model = getattr(record, "model", None) or DEFAULT_MODEL
        quota = call_supported(self.get_quota_snapshot, record.workspace, model=task_model)
        self._set_state(
            task_id,
            quota_after=quota,
            quota_last_checked=quota,
            quota_checked_turn=record.current_turn_index,
            quota_delta=_quota_delta(state.get("quota_before"), quota),
            quota_override_used=override_active,
        )
        if quota.get("blocked") and not override_active:
            reason = self._blocked_message("follow-up", quota)
            self._set_state(
                task_id,
                quota_blocked_next_turn=True,
                quota_blocked_reason=reason,
            )
            return {
                "task_id": task_id,
                "status": "quota_blocked",
                "queued": False,
                "live_interruption": False,
                "turn_index": record.current_turn_index + 1,
                "conversation_id": record.conversation_id,
                "quota_preflight": quota,
                "message": reason,
                "quota_override_used": False,
            }

        self._set_state(
            task_id,
            quota_last_checked=quota,
            quota_blocked_next_turn=False,
            quota_blocked_reason=None,
        )
        result = self.base.steer_task(task_id, instruction)
        next_turn = int(result.get("turn_index") or record.current_turn_index + 1)
        self._start_monitor(task_id, next_turn)
        result["quota_preflight"] = quota
        result["quota_override_used"] = override_active
        return result

    def wait(self, task_id: str, timeout_seconds: float = 30.0) -> dict:
        started = time.monotonic()
        timeout_seconds = max(0.1, min(float(timeout_seconds), server.MAX_WAIT_SECONDS))
        while True:
            remaining = max(0.1, timeout_seconds - (time.monotonic() - started))
            result = self.base.wait_for_task(task_id, remaining)
            if not result.get("finished"):
                return self._attach_state(task_id, result)

            record = self.base.get_task(task_id)
            if not record:
                return result

            returned_turn = int(result.get("current_turn") or 0)
            if (
                record.status in ("started", "running")
                and record.current_turn_index > returned_turn
            ):
                if time.monotonic() - started >= timeout_seconds:
                    return self._attach_state(
                        task_id,
                        {
                            "task_id": task_id,
                            "status": record.status,
                            "finished": False,
                            "conversation_id": record.conversation_id,
                            "current_turn": record.current_turn_index,
                            "session_alive": record.session_alive,
                            "error": record.error,
                        },
                    )
                continue

            if record.status == "completed":
                self._record_post_turn_quota(task_id, record.current_turn_index)
                advanced = self._advance_pending(task_id)
                if advanced:
                    if time.monotonic() - started >= timeout_seconds:
                        record = self.base.get_task(task_id)
                        return self._attach_state(
                            task_id,
                            {
                                "task_id": task_id,
                                "status": record.status,
                                "finished": False,
                                "conversation_id": record.conversation_id,
                                "current_turn": record.current_turn_index,
                                "session_alive": record.session_alive,
                                "error": record.error,
                            },
                        )
                    continue
            return self._attach_state(task_id, result)

    def result(self, task_id: str, max_chars: int = server.DEFAULT_MAX_RESULT_CHARS) -> dict:
        record = self.base.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")
        if record.status in server.TERMINAL_TURN_STATUSES:
            self._record_post_turn_quota(task_id, record.current_turn_index)
        return self._attach_state(task_id, self.base.get_result(task_id, max_chars))

    def status(self, task_id: str) -> dict:
        record = self.base.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")
        state = self._get_state(task_id)
        pending = state.get("pending_instruction")
        return {
            "task_id": record.task_id,
            "status": record.status,
            "conversation_id": record.conversation_id,
            "workspace": record.workspace,
            "current_turn": record.current_turn_index,
            "has_queued_instruction": bool(record.queued_instruction or pending),
            "queued_instruction_preview": (
                (pending or record.queued_instruction)[:100] + "..."
                if (pending or record.queued_instruction)
                and len(pending or record.queued_instruction) > 100
                else (pending or record.queued_instruction)
            ),
            "pid": record.pid,
            "exit_code": record.exit_code,
            "error": record.error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "session_alive": record.session_alive,
            "is_owned": record.task_id in self.base._session_processes,
            **self._public_state(state),
        }

    def cancel(self, task_id: str) -> dict:
        self._set_state(task_id, pending_instruction=None)
        result = self.base.cancel_task(task_id)
        return self._attach_state(task_id, result)

    def _public_state(self, state: dict) -> dict:
        return {
            "quota_before": state.get("quota_before"),
            "quota_after": state.get("quota_after"),
            "quota_delta": state.get("quota_delta"),
            "quota_blocked_next_turn": state.get("quota_blocked_next_turn", False),
            "quota_blocked_reason": state.get("quota_blocked_reason"),
            "quota_pending_instruction": bool(state.get("pending_instruction")),
            "quota_pending_error": state.get("pending_error"),
            "quota_override_used": bool(state.get("quota_override_used", False)),
        }

    def _attach_state(self, task_id: str, result: dict) -> dict:
        return {**result, **self._public_state(self._get_state(task_id))}


coordinator = QuotaCoordinator()


@mcp.tool()
def get_gemini_quota(workspace: str | None = None) -> dict:
    """Refresh Gemini quota without starting an agent turn."""
    return coordinator.get_quota_snapshot(workspace)


@mcp.tool()
def delegate_to_gemini(task: str, workspace: str, quota_override: bool = False) -> dict:
    """Quota-preflight and start one persistent Gemini Antigravity task."""
    return coordinator.delegate(task, workspace, quota_override=quota_override)


@mcp.tool()
def wait_for_task(task_id: str, timeout_seconds: float = 30.0) -> dict:
    """Wait across quota-aware queued turn boundaries."""
    return coordinator.wait(task_id, timeout_seconds)


@mcp.tool()
def get_task_status(task_id: str) -> dict:
    """Inspect task/session state plus Gemini quota state."""
    return coordinator.status(task_id)


@mcp.tool()
def read_task_progress(
    task_id: str, offset: int = 0, limit: int = server.DEFAULT_PROGRESS_LIMIT
) -> dict:
    """Read bounded Antigravity NDJSON progress events."""
    return coordinator.base.read_progress(task_id, offset, limit)


@mcp.tool()
def get_task_result(
    task_id: str, max_chars: int = server.DEFAULT_MAX_RESULT_CHARS
) -> dict:
    """Return Gemini output plus token metrics and before/after quota."""
    return coordinator.result(task_id, max_chars)


@mcp.tool()
def steer_gemini(task_id: str, instruction: str, quota_override: bool = False) -> dict:
    """Queue or start a follow-up only after quota preflight."""
    return coordinator.steer(task_id, instruction, quota_override=quota_override)


@mcp.tool()
def follow_up_gemini(task_id: str, instruction: str, quota_override: bool = False) -> dict:
    """Alias for steer_gemini."""
    return coordinator.steer(task_id, instruction, quota_override=quota_override)


@mcp.tool()
def cancel_task(task_id: str) -> dict:
    """Cancel a running task and discard any quota-aware queued follow-up."""
    return coordinator.cancel(task_id)


if __name__ == "__main__":
    mcp.run()
