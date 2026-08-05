#!/usr/bin/env python3
"""Strict state, command, and Git primitives for codex-autoresearch."""

from __future__ import annotations

import base64
import json
import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


RESULTS_DIR = "autoresearch-results"
RUN_FILE = "run.json"
EVENTS_FILE = "events.jsonl"
PROTECTED_PREFIXES = (
    RESULTS_DIR,
    ".git",
    ".agents/skills/codex-autoresearch",
)


class AutoresearchError(RuntimeError):
    """A user-actionable contract violation."""


@dataclass(frozen=True)
class Paths:
    repo: Path
    root: Path
    run: Path
    events: Path
    logs: Path


@dataclass(frozen=True)
class RunState:
    status: str
    metric: Decimal
    head: str
    iterations: int
    last_event: dict[str, Any]


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    log_path: Path


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def paths_for(repo: Path | str) -> Paths:
    resolved = Path(repo).expanduser().resolve()
    root = resolved / RESULTS_DIR
    return Paths(
        repo=resolved,
        root=root,
        run=root / RUN_FILE,
        events=root / EVENTS_FILE,
        logs=root / "logs",
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_json(text: str, *, source: str, decimal_numbers: bool = False) -> Any:
    options: dict[str, Any] = {
        "object_pairs_hook": _strict_object,
        "parse_constant": _reject_constant,
    }
    if decimal_numbers:
        options["parse_float"] = Decimal
    try:
        return json.loads(text, **options)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AutoresearchError(f"Invalid JSON in {source}: {exc}") from exc


def json_text(value: Any, *, pretty: bool = False) -> str:
    kwargs: dict[str, Any] = {
        "ensure_ascii": True,
        "allow_nan": False,
        "sort_keys": True,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return json.dumps(value, **kwargs)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json_text(payload, pretty=True) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = text.encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_json_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json_text(payload) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise AutoresearchError(f"Failed to append a complete event to {path}")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def parse_decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AutoresearchError(f"{field} must be a finite number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AutoresearchError(f"{field} must be a finite number, got {value!r}") from exc
    if not parsed.is_finite():
        raise AutoresearchError(f"{field} must be finite, got {value!r}")
    return parsed


def decimal_json(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    converted = float(value)
    if not math.isfinite(converted):
        raise AutoresearchError(f"Metric {value} cannot be represented as JSON")
    if Decimal(str(converted)) != value:
        raise AutoresearchError(
            f"Metric {value} would lose precision in JSON; use a representable decimal"
        )
    return converted


def require_exact_keys(
    payload: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
    source: str,
) -> None:
    optional = optional or set()
    missing = sorted(required - payload.keys())
    unknown = sorted(payload.keys() - required - optional)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise AutoresearchError(f"Invalid schema in {source}: {'; '.join(details)}")


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise AutoresearchError(f"git {' '.join(args)} failed: {detail or 'no output'}")
    return completed


def require_git_repo(repo: Path) -> None:
    if not repo.is_dir():
        raise AutoresearchError(f"Repository directory does not exist: {repo}")
    completed = run_git(repo, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode != 0:
        raise AutoresearchError(f"Autoresearch requires a Git repository: {repo}")
    root = Path(completed.stdout.strip()).resolve()
    if root != repo:
        raise AutoresearchError(f"Use the Git repository root {root}, not {repo}")


def git_head(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


def git_branch(repo: Path) -> str:
    completed = run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise AutoresearchError("Autoresearch requires a named Git branch, not detached HEAD")
    return completed.stdout.strip()


def require_git_identity(repo: Path) -> None:
    for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        completed = run_git(repo, "var", identity, check=False)
        if completed.returncode != 0 or not completed.stdout.strip():
            detail = (completed.stderr or completed.stdout).strip()
            raise AutoresearchError(
                f"Git identity {identity} is not configured: {detail or 'no identity returned'}"
            )


def git_status_paths(repo: Path) -> list[str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="strict").strip()
        raise AutoresearchError(f"git status failed: {stderr or 'no output'}")
    try:
        text = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AutoresearchError(f"git status returned non-UTF-8 paths: {exc}") from exc
    tokens = text.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        if len(entry) < 4 or entry[2] != " ":
            raise AutoresearchError(f"Unexpected git status record: {entry!r}")
        status = entry[:2]
        paths.append(entry[3:])
        index += 1
        if "R" in status or "C" in status:
            if index >= len(tokens):
                raise AutoresearchError("Incomplete rename/copy record from git status")
            paths.append(tokens[index])
            index += 1
    return sorted(set(paths))


def is_protected(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)


def is_owned_artifact(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == RESULTS_DIR or normalized.startswith(RESULTS_DIR + "/")


def working_paths(repo: Path) -> list[str]:
    return [path for path in git_status_paths(repo) if not is_owned_artifact(path)]


def staged_paths(repo: Path) -> list[str]:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only", "-z"],
        capture_output=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="strict").strip()
        raise AutoresearchError(f"git diff --cached failed: {stderr or 'no output'}")
    try:
        text = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AutoresearchError(f"git diff --cached returned non-UTF-8 paths: {exc}") from exc
    return sorted(path for path in text.split("\0") if path)


def require_no_staged_artifacts(repo: Path) -> None:
    staged = [path for path in staged_paths(repo) if is_owned_artifact(path)]
    if staged:
        raise AutoresearchError(
            "Autoresearch artifacts must never be staged: " + ", ".join(staged)
        )


def require_artifacts_untracked(repo: Path) -> None:
    completed = run_git(repo, "ls-files", "-z", "--", RESULTS_DIR)
    tracked = [path for path in completed.stdout.split("\0") if path]
    if tracked:
        raise AutoresearchError(
            f"{RESULTS_DIR}/ must remain untracked, but Git tracks: " + ", ".join(tracked)
        )


def require_clean_repo(repo: Path, *, expected_head: str | None = None, expected_branch: str | None = None) -> None:
    dirty = working_paths(repo)
    if dirty:
        raise AutoresearchError(
            "Repository has uncommitted changes: " + ", ".join(dirty) + ". Commit or stash them first."
        )
    if expected_head is not None and git_head(repo) != expected_head:
        raise AutoresearchError(
            f"Git HEAD changed outside autoresearch: expected {expected_head}, got {git_head(repo)}"
        )
    if expected_branch is not None and git_branch(repo) != expected_branch:
        raise AutoresearchError(
            f"Git branch changed outside autoresearch: expected {expected_branch}, got {git_branch(repo)}"
        )


def normalize_scopes(repo: Path, scopes: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for raw in scopes:
        value = raw.strip().replace("\\", "/")
        if not value:
            raise AutoresearchError("Scope paths cannot be empty")
        if any(character in value for character in "*?[]"):
            raise AutoresearchError(
                f"Scope {raw!r} uses a glob. Use repository-relative files or directories instead."
            )
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise AutoresearchError(f"Scope must stay inside the repository: {raw!r}")
        value = value.removeprefix("./").rstrip("/") or "."
        resolved = (repo / value).resolve()
        try:
            resolved.relative_to(repo)
        except ValueError as exc:
            raise AutoresearchError(f"Scope escapes the repository: {raw!r}") from exc
        if is_protected(value):
            raise AutoresearchError(f"Scope cannot target autoresearch or Git internals: {raw!r}")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise AutoresearchError("At least one scope path is required")
    return normalized


def path_in_scope(path: str, scopes: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    for scope in scopes:
        if scope == "." or normalized == scope or normalized.startswith(scope + "/"):
            return True
    return False


def require_paths_in_scope(paths: list[str], scopes: list[str]) -> None:
    protected = [path for path in paths if is_protected(path)]
    if protected:
        raise AutoresearchError("Protected paths were modified: " + ", ".join(protected))
    outside = [path for path in paths if not path_in_scope(path, scopes)]
    if outside:
        raise AutoresearchError("Out-of-scope paths were modified: " + ", ".join(outside))


def _record_command_output(
    *,
    command: str,
    cwd: Path,
    duration_seconds: float,
    returncode: int | None,
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    timed_out: bool,
    log_path: Path,
    termination_error: str | None = None,
) -> tuple[str | None, str | None, list[str]]:
    payload: dict[str, Any] = {
        "command": command,
        "cwd": str(cwd),
        "duration_seconds": round(duration_seconds, 6),
        "returncode": returncode,
        "timed_out": timed_out,
    }
    decoded: dict[str, str | None] = {}
    encoding_errors: list[str] = []
    for stream, data in (("stdout", stdout_bytes), ("stderr", stderr_bytes)):
        try:
            decoded[stream] = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            decoded[stream] = None
            payload[f"{stream}_base64"] = base64.b64encode(data).decode("ascii")
            encoding_errors.append(f"{stream}: {exc}")
    payload.update(decoded)
    if encoding_errors:
        payload["encoding_errors"] = encoding_errors
    if termination_error is not None:
        payload["termination_error"] = termination_error
    write_json_atomic(log_path, payload)
    return decoded["stdout"], decoded["stderr"], encoding_errors


def run_command(
    *,
    command: str,
    cwd: Path,
    timeout_seconds: int,
    log_path: Path,
) -> CommandResult:
    started = time.monotonic()
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as timeout_error:
        termination_error: str | None = None
        try:
            terminate_process_tree(process)
        except AutoresearchError as exc:
            termination_error = str(exc)
            stdout_bytes = timeout_error.output or b""
            stderr_bytes = timeout_error.stderr or b""
        else:
            stdout_bytes, stderr_bytes = process.communicate()
        duration = time.monotonic() - started
        _, _, encoding_errors = _record_command_output(
            command=command,
            cwd=cwd,
            duration_seconds=duration,
            returncode=process.returncode,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            timed_out=True,
            log_path=log_path,
            termination_error=termination_error,
        )
        details = []
        if encoding_errors:
            details.append("output was not valid UTF-8")
        if termination_error is not None:
            details.append(f"process-tree termination failed: {termination_error}")
        suffix = f"; {'; '.join(details)}" if details else ""
        raise AutoresearchError(
            f"Command timed out after {timeout_seconds}s: {command}{suffix}. "
            f"Full output: {log_path}"
        )
    duration = time.monotonic() - started
    stdout, stderr, encoding_errors = _record_command_output(
        command=command,
        cwd=cwd,
        duration_seconds=duration,
        returncode=process.returncode,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        timed_out=False,
        log_path=log_path,
    )
    if encoding_errors:
        raise AutoresearchError(
            f"Command produced non-UTF-8 output ({'; '.join(encoding_errors)}): {command}. "
            f"Raw output is base64-encoded in {log_path}"
        )
    if stdout is None or stderr is None:
        raise AutoresearchError(f"Command output decoder returned an invalid state: {log_path}")
    return CommandResult(
        command=command,
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        log_path=log_path,
    )


def parse_metric_output(result: CommandResult, *, json_key: str | None) -> Decimal:
    if result.returncode != 0:
        raise AutoresearchError(
            f"Metric command exited {result.returncode}: {result.command}. Full output: {result.log_path}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AutoresearchError(
            f"Metric command produced no non-empty stdout line. Full output: {result.log_path}"
        )
    final_line = lines[-1].strip()
    if json_key is None:
        return parse_decimal(final_line, field="metric command final stdout line")
    payload = parse_json(
        final_line,
        source=f"final stdout line from {result.command!r}",
        decimal_numbers=True,
    )
    if not isinstance(payload, dict):
        raise AutoresearchError("Metric JSON output must be an object")
    if json_key not in payload:
        raise AutoresearchError(f"Metric JSON output is missing key {json_key!r}")
    return parse_decimal(payload[json_key], field=f"metric JSON key {json_key!r}")


def improved(candidate: Decimal, retained: Decimal, direction: str) -> bool:
    return candidate < retained if direction == "lower" else candidate > retained


def target_reached(metric: Decimal, target: Decimal, direction: str) -> bool:
    return metric <= target if direction == "lower" else metric >= target


def relative_log_path(paths: Paths, path: Path | None) -> str | None:
    if path is None:
        return None
    return str(path.relative_to(paths.root))


def next_command_log(paths: Paths, iteration: int, kind: str) -> Path:
    return paths.logs / f"{iteration:04d}-{kind}.json"


def commit_trial(repo: Path, *, paths: list[str], description: str) -> str:
    title = " ".join(description.split())
    if not title:
        raise AutoresearchError("Iteration description cannot be empty")
    title = title[:64].rstrip()
    literal_paths = [f":(literal){path}" for path in paths]
    run_git(repo, "add", "-A", "--", *literal_paths)
    run_git(repo, "commit", "-m", f"autoresearch: {title}")
    remaining = working_paths(repo)
    if remaining:
        raise AutoresearchError(
            "Working tree changed while creating the trial commit: " + ", ".join(remaining)
        )
    return git_head(repo)


def revert_trial(repo: Path, trial_commit: str) -> str:
    run_git(repo, "revert", "--no-edit", trial_commit)
    remaining = working_paths(repo)
    if remaining:
        raise AutoresearchError("Rollback left uncommitted paths: " + ", ".join(remaining))
    return git_head(repo)


def process_group_alive(process_group_id: int) -> bool:
    if os.name == "nt" or process_group_id <= 0:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_tree(
    process: subprocess.Popen[Any],
    *,
    grace_seconds: float = 5.0,
) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            return
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
        )
        if completed.returncode != 0 and process.poll() is None:
            detail = (completed.stderr or completed.stdout).strip()
            raise AutoresearchError(
                f"Failed to terminate process tree {process.pid}: {detail or 'no output'}"
            )
        try:
            process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired as final_exc:
                raise AutoresearchError(
                    f"Process tree {process.pid} did not exit after forced termination"
                ) from final_exc
            return

    if not process_group_alive(process.pid):
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired as exc:
                raise AutoresearchError(
                    f"Process {process.pid} did not exit after direct termination"
                ) from exc
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_seconds
    while process_group_alive(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if process_group_alive(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + grace_seconds
    while process_group_alive(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if process_group_alive(process.pid):
        raise AutoresearchError(f"Process tree {process.pid} did not exit after TERM and KILL")
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired as exc:
        raise AutoresearchError(f"Process {process.pid} was not reaped after tree termination") from exc
