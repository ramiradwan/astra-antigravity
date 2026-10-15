"""Noninteractive CLI probes with bounded waits and captured output."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence

from process_lifecycle import stop_owned_process

MAX_PROBE_OUTPUT_BYTES = 64 * 1024


class ProbeOutputLimitError(RuntimeError):
    pass


def run_probe(
    args: Sequence[str], *, timeout: float = 5.0,
    max_output_bytes: int = MAX_PROBE_OUTPUT_BYTES,
) -> subprocess.CompletedProcess:
    """Never inherit MCP input or wait for EOF on a descendant's output pipe.

    File capture keeps output out of memory until the owned process terminates.
    Oversized output fails closed rather than advertising a partial model list.
    """
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("Probe timeout and output limit must be positive")
    command = list(args)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        try:
            proc.wait(timeout=timeout)
        except BaseException:
            stop_owned_process(proc)
            raise
        size = stdout.seek(0, os.SEEK_END) + stderr.seek(0, os.SEEK_END)
        if size > max_output_bytes:
            raise ProbeOutputLimitError(f"Probe output exceeded {max_output_bytes} bytes")
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(
            command, proc.returncode,
            stdout.read(max_output_bytes).decode("utf-8", errors="replace"),
            stderr.read(max_output_bytes).decode("utf-8", errors="replace"),
        )
