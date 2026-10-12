"""Run a command with a deadline that actually stops it, descendants included.

`subprocess.run(..., timeout=)` kills only the process it started. On Windows that
is the wrong process: OpenCode and the project's test runner are launched through
shims that start `node`, so at the deadline the shim dies and the real work carries
on — a `hardhat test` left running while the Judge starts its own in the same
directory, or holding files so the workspace cannot even be deleted.

`taskkill /T` is not enough either, and that was measured, not assumed: it walks
parent-process links, and a process whose parent has already exited is no longer
anyone's child. OpenCode's shell tool starts `npx`, which starts `node` and exits;
the `hardhat` that outlived the 2026-10-12 Attempt's deadline (and its `solc`, still
burning CPU two minutes later) was exactly that. A Windows **Job Object** is the tool
for this: every process started inside it belongs to it for good, however it is
re-parented, and terminating the job ends them all.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

if sys.platform == "win32":

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]


class _Job:
    """A Windows Job Object holding one process and everything it ever starts."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def containing(cls, process: subprocess.Popen) -> _Job | None:
        """A job holding `process`, or None where that isn't possible (not Windows, or refused)."""
        if sys.platform != "win32":
            return None
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        limits = _ExtendedLimits()
        # Closing the last handle to the job ends everything in it, so even a crash of
        # this process cannot leave a build running behind it.
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = _kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        assigned = configured and _kernel32.AssignProcessToJobObject(handle, int(process._handle))
        if not assigned:
            _kernel32.CloseHandle(handle)
            return None
        return cls(handle)

    @staticmethod
    def resume(process: subprocess.Popen) -> None:
        _ntdll.NtResumeProcess(int(process._handle))

    def terminate(self) -> None:
        _kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        _kernel32.CloseHandle(self._handle)


def kill_process_tree(process: subprocess.Popen) -> None:
    """Stop `process` and the children it can still be traced to. The fallback where no job holds it."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()
    process.wait()


def run_killing_tree(
    args: Sequence[str], *, cwd: Path, timeout_seconds: float, **popen_kwargs: Any
) -> int | None:
    """The command's exit code, or None if the deadline passed and it was stopped, descendants included.

    Whatever the command started is gone when this returns, however it ended: a build
    left running behind a finished or killed command would hold the workspace's files
    and compete with the next step for the same directory.
    """
    if sys.platform == "win32":
        # Started suspended so it can be put in its job before it runs. Without this a
        # virtualenv's `python.exe` (a launcher stub that starts the real interpreter at
        # once) has already spawned its child by the time the job exists, and that child
        # — the process actually doing the work — never joins it.
        popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | _CREATE_SUSPENDED
    process = subprocess.Popen(list(args), cwd=cwd, **popen_kwargs)
    job = _Job.containing(process)
    if sys.platform == "win32":
        _Job.resume(process)  # whether or not a job took it: it must run either way
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # The traceable tree first, while its root is still alive to be traced from; then
        # the job, which also holds what a dead parent left behind.
        kill_process_tree(process)
        if job is not None:
            job.terminate()
        return None
    finally:
        if job is not None:
            job.close()  # KILL_ON_JOB_CLOSE: ends any descendant still running after a normal exit
