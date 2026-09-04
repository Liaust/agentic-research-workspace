"""Capability-checked, receipt-backed Codex CLI execution."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate  # type: ignore[import-untyped]

from research_map.receipts import fingerprint, write_receipt
from research_map.workspace import validate_workspace

CODEX_RUNNER_VERSION = "1.2"
_STDERR_TAIL_LIMIT = 65_536
_TERMINAL_EVENT_TYPES = frozenset({"turn.completed", "turn.failed"})
_REQUIRED_EXEC_OPTIONS = (
    "--model",
    "--ephemeral",
    "--ignore-user-config",
    "--json",
    "--sandbox",
    "--cd",
    "--output-schema",
    "--output-last-message",
)


class CodexCapabilityError(RuntimeError):
    """Raised when the configured Codex executable lacks the pilot contract."""


@dataclass(frozen=True, slots=True)
class CodexCapabilities:
    executable: str
    version: str
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexJob:
    job_id: str
    source_id: str
    model: str
    prompt: str
    workspace: Path
    output_schema: Path | None
    events_path: Path
    last_message_path: Path | None
    receipt_path: Path
    authoritative_output_path: Path | None = None
    authoritative_output_schema: Path | None = None
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class CodexRunResult:
    returncode: int
    output_valid: bool
    validation_errors: tuple[str, ...]
    receipt_path: Path
    arguments: tuple[str, ...]
    failure_class: str | None
    duration_seconds: float
    event_count: int
    terminal_event_seen: bool


def check_capabilities(executable: str = "codex") -> CodexCapabilities:
    try:
        version = subprocess.run(
            [executable, "--version"], check=False, capture_output=True, text=True
        )
        help_result = subprocess.run(
            [executable, "exec", "--help"], check=False, capture_output=True, text=True
        )
    except OSError as error:
        raise CodexCapabilityError(f"Codex executable unavailable: {error}") from error
    if version.returncode != 0:
        raise CodexCapabilityError("Codex version check failed")
    if help_result.returncode != 0:
        raise CodexCapabilityError("Codex exec capability check failed")
    missing = tuple(option for option in _REQUIRED_EXEC_OPTIONS if option not in help_result.stdout)
    if missing:
        raise CodexCapabilityError(f"Codex exec options missing: {', '.join(missing)}")
    return CodexCapabilities(
        executable=executable,
        version=version.stdout.strip(),
        options=_REQUIRED_EXEC_OPTIONS,
    )


class CodexRunner:
    def __init__(self, executable: str = "codex") -> None:
        self.capabilities = check_capabilities(executable)

    def run(self, job: CodexJob) -> CodexRunResult:
        """Run one ephemeral job, retaining output and a receipt on every outcome."""

        workspace = validate_workspace(job.workspace, expected_source_id=job.source_id)
        if not job.model.strip():
            raise ValueError("Codex job requires an explicit model")
        output_mode, output_path, schema_path = _resolve_output_contract(job, workspace.path)
        if not schema_path.is_file():
            raise ValueError(f"output schema is missing: {schema_path}")
        job.events_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        argument_list = [
            self.capabilities.executable,
            "exec",
            "--model",
            job.model,
            "--ephemeral",
            "--ignore-user-config",
            "--json",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(job.workspace),
        ]
        if output_mode == "structured_last_message":
            argument_list.extend(
                (
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                )
            )
        argument_list.extend(("-c", "sandbox_workspace_write.network_access=false", job.prompt))
        arguments = tuple(argument_list)
        job.events_path.write_text("", encoding="utf-8")
        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        timed_out = False
        process_error: str | None = None
        returncode = 127
        stderr = ""
        stderr_truncated = False
        event_state = _EventState()
        termination: dict[str, Any] | None = None
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=job.workspace,
                start_new_session=True,
            )
        except OSError as error:
            process_error = str(error)
        else:
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_thread = threading.Thread(
                target=_stream_events,
                args=(process.stdout, job.events_path, event_state),
                name=f"codex-events-{job.job_id}",
                daemon=True,
            )
            stderr_state = _StderrState()
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(process.stderr, stderr_state),
                name=f"codex-stderr-{job.job_id}",
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            try:
                returncode = process.wait(timeout=job.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                termination = _terminate_process_tree(process)
                returncode = 124
                process_error = f"worker timed out after {job.timeout_seconds} seconds"
            stdout_thread.join()
            stderr_thread.join()
            stderr = stderr_state.tail
            stderr_truncated = stderr_state.truncated

        finished_at = datetime.now(UTC)
        duration_seconds = time.monotonic() - started_monotonic
        validation_errors = list(event_state.errors)
        if event_state.terminal_event_type is None:
            validation_errors.append("Codex event stream did not contain a terminal turn event")
        elif event_state.terminal_event_type == "turn.failed":
            validation_errors.append("Codex reported a terminal failure event")
        if returncode != 0:
            validation_errors.append(f"worker exited with status {returncode}")
        proposal: Any = None
        output_schema_valid = False
        if output_path.is_file():
            try:
                proposal = json.loads(output_path.read_text(encoding="utf-8"))
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                validate(instance=proposal, schema=schema)
                output_schema_valid = True
            except (OSError, json.JSONDecodeError, ValidationError) as error:
                validation_errors.append(str(error))
        else:
            missing_output = (
                "last-message output"
                if output_mode == "structured_last_message"
                else "authoritative output artifact"
            )
            validation_errors.append(f"Codex did not write the {missing_output}")
        if process_error is not None:
            validation_errors.append(process_error)

        failure_class = _failure_class(
            timed_out=timed_out,
            process_error=process_error,
            returncode=returncode,
            event_errors=event_state.errors,
            terminal_event_type=event_state.terminal_event_type,
            output_exists=output_path.is_file(),
            output_schema_valid=output_schema_valid,
            output_mode=output_mode,
        )

        output_fingerprints = [fingerprint(job.events_path).to_dict()]
        if output_path.is_file():
            output_fingerprints.append(fingerprint(output_path).to_dict())
        input_fingerprints = [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
            }
            for item in workspace.manifest["inputs"]
        ]
        write_receipt(
            job.receipt_path,
            {
                "kind": "codex_job",
                "runner_version": CODEX_RUNNER_VERSION,
                "job_id": job.job_id,
                "source_id": job.source_id,
                "codex_version": self.capabilities.version,
                "model": job.model,
                "arguments": list(arguments[1:-1]) + ["<prompt>"],
                "prompt": job.prompt,
                "workspace_manifest_sha256": fingerprint(workspace.manifest_path).sha256,
                "output_schema": fingerprint(schema_path).to_dict(),
                "output_contract": {
                    "mode": output_mode,
                    "path": str(output_path),
                },
                "inputs": input_fingerprints,
                "process": {
                    "returncode": returncode,
                    "stderr": stderr,
                    "stderr_truncated": stderr_truncated,
                    "timed_out": timed_out,
                    "timeout_seconds": job.timeout_seconds,
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "duration_seconds": duration_seconds,
                    "termination": termination,
                },
                "events": {
                    "count": event_state.count,
                    "last_event": event_state.last_event,
                    "terminal_event_seen": event_state.terminal_event_type is not None,
                    "terminal_event_type": event_state.terminal_event_type,
                },
                "proposal": proposal if output_mode == "structured_last_message" else None,
                "validation": {
                    "valid": returncode == 0 and not validation_errors,
                    "errors": validation_errors,
                    "failure_class": failure_class,
                },
                "outputs": output_fingerprints,
            },
        )
        return CodexRunResult(
            returncode=returncode,
            output_valid=returncode == 0 and not validation_errors,
            validation_errors=tuple(validation_errors),
            receipt_path=job.receipt_path,
            arguments=arguments,
            failure_class=failure_class,
            duration_seconds=duration_seconds,
            event_count=event_state.count,
            terminal_event_seen=event_state.terminal_event_type is not None,
        )


@dataclass(slots=True)
class _EventState:
    count: int = 0
    last_event: dict[str, Any] | None = None
    terminal_event_type: str | None = None
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class _StderrState:
    tail: str = ""
    truncated: bool = False


def _stream_events(stream: Any, events_path: Path, state: _EventState) -> None:
    errors: list[str] = []
    with events_path.open("w", encoding="utf-8", buffering=1) as events:
        for number, line in enumerate(stream, start=1):
            events.write(line)
            events.flush()
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"event line {number} is not JSON")
                continue
            if not isinstance(event, dict):
                errors.append(f"event line {number} is not an object")
                continue
            state.count += 1
            state.last_event = event
            event_type = event.get("type")
            if event_type in _TERMINAL_EVENT_TYPES:
                state.terminal_event_type = str(event_type)
    state.errors = tuple(errors)


def _drain_stderr(stream: Any, state: _StderrState) -> None:
    while chunk := stream.read(8192):
        combined = state.tail + chunk
        if len(combined) > _STDERR_TAIL_LIMIT:
            state.truncated = True
            combined = combined[-_STDERR_TAIL_LIMIT:]
        state.tail = combined


def _terminate_process_tree(process: subprocess.Popen[str]) -> dict[str, Any]:
    termination: dict[str, Any] = {
        "scope": "process_group" if os.name == "posix" else "process",
        "terminate_sent": False,
        "kill_sent": False,
    }
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        termination["terminate_sent"] = True
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            termination["kill_sent"] = True
        except ProcessLookupError:
            pass
        process.wait()
    termination["process_returncode"] = process.returncode
    return termination


def _failure_class(
    *,
    timed_out: bool,
    process_error: str | None,
    returncode: int,
    event_errors: tuple[str, ...],
    terminal_event_type: str | None,
    output_exists: bool,
    output_schema_valid: bool,
    output_mode: str,
) -> str | None:
    if timed_out:
        return "worker_timeout"
    if process_error is not None:
        return "worker_start_failed"
    if returncode != 0:
        return "worker_exit"
    if event_errors:
        return "event_stream_invalid"
    if terminal_event_type is None:
        return "transport_without_terminal_event"
    if terminal_event_type == "turn.failed":
        return "worker_terminal_failure"
    if not output_exists:
        return (
            "structured_last_message_missing"
            if output_mode == "structured_last_message"
            else "authoritative_artifact_missing"
        )
    if not output_schema_valid:
        return "artifact_schema_invalid"
    return None


def _resolve_output_contract(job: CodexJob, workspace: Path) -> tuple[str, Path, Path]:
    structured_values = (job.output_schema, job.last_message_path)
    artifact_values = (job.authoritative_output_schema, job.authoritative_output_path)
    structured_complete = all(value is not None for value in structured_values)
    artifact_complete = all(value is not None for value in artifact_values)
    if any(value is not None for value in structured_values) and not structured_complete:
        raise ValueError("structured output requires both output_schema and last_message_path")
    if any(value is not None for value in artifact_values) and not artifact_complete:
        raise ValueError(
            "authoritative output requires both authoritative_output_schema and "
            "authoritative_output_path"
        )
    if structured_complete == artifact_complete:
        raise ValueError(
            "Codex job requires exactly one output contract: structured last message "
            "or authoritative workspace artifact"
        )
    if structured_complete:
        assert job.last_message_path is not None
        assert job.output_schema is not None
        return "structured_last_message", job.last_message_path, job.output_schema

    assert job.authoritative_output_path is not None
    assert job.authoritative_output_schema is not None
    resolved_output = job.authoritative_output_path.resolve()
    try:
        resolved_output.relative_to(workspace.resolve())
    except ValueError as error:
        raise ValueError("authoritative output must be inside the job workspace") from error
    return "workspace_artifact", resolved_output, job.authoritative_output_schema
