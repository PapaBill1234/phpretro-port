#!/usr/bin/env python3
"""Linux process-group guardian for the native app-server (prerequisite).

A tiny, single-purpose wrapper that launches ONE host-fixed native argv as a
member of the guardian's OWN process group and guarantees that group is
reclaimed when the owning host dies, the shared work deadline passes, the group
is signalled, or the native process exits.

Why it exists
-------------
``RpcClient`` starts its child detached (``start_new_session=True``) and closes
it by signalling the leader's process group. That covers a cooperative close,
but on a hard parent death the detached native group can be orphaned: the pinned
app-server stdio transport only starts a 45 s watchdog while asynchronous
teardown continues, so nothing promptly kills a detached native group. This
guardian is the reviewed prerequisite that makes the group reclaimable.

Contract (host-fixed; a caller request JSON can never choose any of these)
-------------------------------------------------------------------------
* Launched by the reviewed ``RpcClient`` as
  ``[sys.executable, native-group-guardian.py, --parent-pid P,
  --deadline D, -- <native argv...>]`` with ``start_new_session=True``, so the
  guardian is the leader of a fresh session/process group and OWNS that group.
* ``P`` is the host PID (``os.getpid()`` captured host-side) and ``D`` an
  ABSOLUTE ``CLOCK_MONOTONIC`` deadline. Neither comes from the request JSON.
* The native argv is host-fixed. The guardian stays the group leader and spawns
  the native process with inherited stdin/stdout and
  ``start_new_session=False``, so the RpcClient speaks JSON-RPC directly to the
  native process.
* The guardian writes NOTHING to stdout — that channel belongs exclusively to
  the native RPC stream. stderr is limited to a fixed sanitized token and never
  contains argv, env or config.
* Guardian exit NEVER establishes successful JSON-RPC completion. Only the
  native stream's exact ``turn/completed`` is trusted; a guardian exit (or its
  exit code) is never a success, and a detach/EOF surfaces as an RPC error.

Lifecycle guards
----------------
* A ``SIGTERM`` handler is installed BEFORE ``PR_SET_PDEATHSIG(SIGTERM)`` and the
  ``prctl`` result is checked, so a hard parent death raises a catchable signal
  rather than an unhandled default.
* The expected parent is checked immediately before spawn and again after, and
  an invalid parent/deadline or an unsupported platform/prctl fails BEFORE the
  native process is launched.
* The monitor is a bounded poll (deadline + parent + child + signal), never an
  indefinite wait. On parent death, deadline, TERM, native exit or a startup
  failure the owned group is reclaimed: ``SIGTERM`` the group, a FIXED bounded
  grace (<= 1 s), then ``SIGKILL`` the whole group INCLUDING this guardian so
  the group id cannot be reused while any member is still unreachable.
* Further ``SIGTERM`` is ignored during cleanup, so a self-group TERM or a
  repeated signal cannot recurse or extend the fixed grace window.

Known limits (not claimed): a process that deliberately escapes the group
(``setsid``), or a kernel-uninterruptible (``D``) process, is not reclaimable
here, and global cleanup is not asserted.
"""
from __future__ import annotations

import ctypes
import math
import os
import signal
import subprocess
import sys
import time

PR_SET_PDEATHSIG = 1
# Fixed, bounded grace between the polite group TERM and the final group KILL.
CLEANUP_GRACE = 0.5
POLL_INTERVAL = 0.02

# Pre-launch refusals. Every exit is a failure and nonzero; there is no success
# exit code — a guardian exit never establishes successful JSON-RPC completion.
EXIT_USAGE = 2
EXIT_PLATFORM = 3
EXIT_PARENT = 4
EXIT_DEADLINE = 5
EXIT_PRCTL = 6
EXIT_SPAWN = 7
EXIT_GROUP = 8
EXIT_SIGNALLED = 9
EXIT_CLEANUP_FAILED = 10


def parse_argv(argv):
    """Split ``--parent-pid P --deadline D -- <native argv...>``.

    Raises ``ValueError`` for anything else. The host fixes both values; a
    request cannot reach this parser.
    """
    fields = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token in ("--parent-pid", "--deadline"):
            if index + 1 >= len(argv):
                raise ValueError("missing value for " + token)
            fields[token] = argv[index + 1]
            index += 2
        else:
            raise ValueError("unexpected argument")
    native = list(argv[index:])
    if set(fields) != {"--parent-pid", "--deadline"} or not native:
        raise ValueError("requires --parent-pid, --deadline and a native argv")
    try:
        parent = int(fields["--parent-pid"])
        deadline = float(fields["--deadline"])
    except (TypeError, ValueError):
        raise ValueError("invalid numeric argument") from None
    return parent, deadline, native


def set_pdeathsig(signum=signal.SIGTERM):
    """Set ``PR_SET_PDEATHSIG`` and return True only on confirmed success.

    Any load/lookup failure or a nonzero ``prctl`` return is a refusal the
    caller turns into a pre-launch failure.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return False
    try:
        libc.prctl.restype = ctypes.c_int
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
    except (AttributeError, TypeError):
        return False
    return libc.prctl(PR_SET_PDEATHSIG, int(signum), 0, 0, 0) == 0


def _signal_group(signum):
    """Signal every member of this guardian's process group (never raises)."""
    try:
        os.killpg(os.getpgid(0), signum)
    except OSError:
        pass


def _running_group_members(exclude):
    """PIDs still running in this process group, excluding ``exclude``/zombies.

    ``None`` means "could not determine", which the caller treats as "keep the
    full bounded grace". Zombie (``Z``) and dead (``X``) states are not running.
    """
    try:
        pgid = os.getpgid(0)
        names = os.listdir("/proc")
    except OSError:
        return None
    members = []
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == exclude:
            continue
        try:
            with open("/proc/%d/stat" % pid, "rb") as handle:
                blob = handle.read()
        except OSError:
            continue
        close = blob.rfind(b")")
        if close < 0:
            continue
        fields = blob[close + 1 :].split()
        if len(fields) < 3:
            continue
        state = fields[0]
        try:
            if int(fields[2]) != pgid:
                continue
        except ValueError:
            continue
        if state in (b"Z", b"X"):
            continue
        members.append(pid)
    return members


def reclaim(native):
    """TERM the owned group, wait a FIXED bounded grace, then KILL it and self.

    Idempotent via a module flag. SIGTERM is ignored for the duration so a
    self-group TERM or a repeated signal cannot recurse or extend the grace.
    """
    global _CLEANING
    if _CLEANING:
        return
    _CLEANING = True
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    _signal_group(signal.SIGTERM)
    end = time.monotonic() + CLEANUP_GRACE
    while True:
        if native is not None:
            native.poll()  # reap the native child if it has exited
        if _running_group_members(os.getpid()) == []:
            break
        if time.monotonic() >= end:
            break
        time.sleep(POLL_INTERVAL)
    # Final whole-group KILL INCLUDING this guardian: the leader stays alive
    # through the signal so the group id is signalled as a unit and only then
    # released, preventing reuse while a member is still unreachable.
    _signal_group(signal.SIGKILL)
    # Only reachable if the group KILL failed to deliver to this process (for
    # example a lost group). It is a cleanup FAILURE and must exit nonzero.
    os._exit(EXIT_CLEANUP_FAILED)


_CLEANING = False
_TERMINATED = False


def _on_term(_signum, _frame):
    global _TERMINATED
    _TERMINATED = True


def _monitor(native, parent, deadline):
    """Bounded poll until a terminal condition; never an indefinite wait."""
    while True:
        if _TERMINATED:
            return
        if native.poll() is not None:
            return
        if os.getppid() != parent:
            return
        if time.monotonic() >= deadline:
            return
        try:
            native.wait(timeout=POLL_INTERVAL)
        except subprocess.TimeoutExpired:
            pass


def _owns_group():
    """True only if this process is BOTH the group leader and the session leader.

    A caller only reaches this process through the reviewed detached launch
    (``start_new_session=True``). Anything else shares a caller's group, where a
    cleanup TERM/KILL would hit unrelated processes.
    """
    try:
        return os.getpid() == os.getpgrp() and os.getpid() == os.getsid(0)
    except OSError:
        return False


def _refuse(parent, deadline):
    """A refusal code to fail BEFORE/AROUND a launch, or ``None`` to proceed.

    Re-checks the pre-spawn signal flag, the expected parent AND the original
    deadline, so a TERM/parent change/deadline reached during the prctl startup
    window cannot be followed by a native launch.
    """
    if _TERMINATED:
        return EXIT_SIGNALLED
    if os.getppid() != parent:
        return EXIT_PARENT
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        return EXIT_DEADLINE
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        parent, deadline, native_argv = parse_argv(argv)
    except ValueError:
        return EXIT_USAGE
    if sys.platform != "linux":
        return EXIT_PLATFORM
    if parent <= 1 or parent == os.getpid():
        return EXIT_PARENT
    # The guardian must own BOTH the process group AND the session, exactly as
    # the reviewed RpcClient starts it (``start_new_session=True``). Otherwise a
    # standalone/GROUP invocation is sharing a caller's group and cleanup would
    # TERM/KILL unrelated processes; refuse before any launch.
    if not _owns_group():
        return EXIT_GROUP
    if not math.isfinite(deadline) or deadline - time.monotonic() <= 0:
        return EXIT_DEADLINE
    # SIGTERM handler BEFORE PDEATHSIG so a hard parent death is catchable.
    signal.signal(signal.SIGTERM, _on_term)
    if not set_pdeathsig(signal.SIGTERM):
        return EXIT_PRCTL
    # The parent can die in the race between fork and prctl; PDEATHSIG then never
    # fires, so re-check signal/parent/deadline immediately before launch.
    refusal = _refuse(parent, deadline)
    if refusal is not None:
        return refusal
    try:
        native = subprocess.Popen(native_argv, start_new_session=False)
    except OSError:
        return EXIT_SPAWN
    # ...and again after launch; a refusal here reclaims the just-spawned group.
    refusal = _refuse(parent, deadline)
    if refusal is not None:
        reclaim(native)
        return refusal
    _monitor(native, parent, deadline)
    reclaim(native)
    # Unreachable: reclaim always ends in the whole-group KILL (or its nonzero
    # fallback). Never return 0 — a guardian exit is never RPC success.
    return EXIT_CLEANUP_FAILED


if __name__ == "__main__":
    sys.exit(main())
