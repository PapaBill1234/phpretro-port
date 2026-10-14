"""Process tests for the Linux native group guardian (G05 prerequisite).

Every scenario is driven by REAL, controlled OS processes: a fake native and a
fake grandchild (plain Python), and a Python "parent" whose hard death is
simulated. A small set of REFUSAL-WINDOW cases mock the guardian's destructive
primitives so the test runner's own process group is never signalled. NOTHING
here logs in, launches the native Codex/app-server, calls a model, or touches
the network or an account.

Process identity is tracked through ``/proc/<pid>/stat``. Only ``ENOENT`` /
``ESRCH`` mean a pid is GONE; a permission/I/O failure or malformed/short stat
RAISES so an unreadable state can never be mistaken for a stopped process. A
process is only classified as not-running after a valid ``stat`` parse with a
matching start-tick identity, so a RECYCLED pid is never accepted as a survivor.

Limitations (NOT claimed): a process that deliberately escapes the group
(``setsid``) or a kernel-uninterruptible (``D``) process is not reclaimable here,
and no GLOBAL cleanup proof is asserted.
"""
import errno
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
GUARDIAN = str(ROOT / "scripts" / "native-group-guardian.py")
THIS = sys.modules[__name__]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guardian = _load("native_group_guardian", "scripts/native-group-guardian.py")
generate = _load("codex_generate", "scripts/codex-generate.py")


# --- pid identity helpers -----------------------------------------------------

class StatError(AssertionError):
    """The pid state could not be read for a reason OTHER than the pid being gone."""


def _read_stat_bytes(pid):
    """Raw ``/proc/<pid>/stat`` bytes. Indirection so tests can inject faults."""
    with open("/proc/%d/stat" % pid, "rb") as handle:
        return handle.read()


def _stat_fields(pid):
    """Fields after ``comm`` in ``/proc/<pid>/stat``.

    Returns ``None`` ONLY when the pid is genuinely gone (``ENOENT``/``ESRCH``).
    A permission or I/O failure, or malformed/short content, RAISES: an
    unreadable state must never be reported as a stopped one.
    """
    try:
        blob = _read_stat_bytes(pid)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ESRCH):
            return None
        raise StatError(
            "cannot read /proc/%d/stat (%s): %r"
            % (pid, errno.errorcode.get(exc.errno, exc.errno), exc)
        ) from exc
    close = blob.rfind(b")")
    if close < 0:
        raise StatError("malformed /proc/%d/stat (no comm terminator): %r" % (pid, blob))
    fields = blob[close + 1:].split()
    if len(fields) < 20:
        raise StatError("short /proc/%d/stat (%d fields): %r" % (pid, len(fields), blob))
    if fields[0] not in (b"R", b"S", b"D", b"Z", b"T", b"t", b"W", b"X", b"x", b"K", b"P", b"I"):
        raise StatError("invalid observed state %r for pid %d" % (fields[0], pid))
    for index in (1, 2, 3, 19):
        if not fields[index].isdigit():
            raise StatError("invalid numeric stat field %d for pid %d" % (index + 3, pid))
    return fields


def _identity(pid):
    """A valid start-tick (field 22) for a pid still present in ``/proc``.

    RAISES when the pid is gone or its record is unreadable/malformed: a None or
    bogus baseline would make every later check look 'gone' and silently pass.
    """
    fields = _stat_fields(pid)
    if fields is None:
        raise StatError("pid %d is gone; no initial start-tick record" % pid)
    tick = fields[19]
    if not tick.isdigit():
        raise StatError("invalid start-tick %r for pid %d" % (tick, pid))
    return tick


def _identity_if_recorded(pid):
    """Start tick, or ``None`` ONLY when the pid is genuinely gone (ENOENT/ESRCH)."""
    fields = _stat_fields(pid)
    return None if fields is None else fields[19]


def _state(pid):
    fields = _stat_fields(pid)
    return None if fields is None else fields[0]


def _pgrp(pid):
    fields = _stat_fields(pid)
    return None if fields is None else fields[2]


def _session(pid):
    fields = _stat_fields(pid)
    return None if fields is None else fields[3]


def _running(pid, start):
    """``True`` running, ``False`` a corpse/zombie/X, ``None`` gone or recycled.

    RAISES when the state is unreadable/malformed, or when ``start`` is not a
    valid initial start-tick record: only a valid parse with a matching identity
    can classify the process. A recycled pid (identity mismatch) is ``None``.
    """
    if not isinstance(start, bytes) or not start.isdigit():
        raise StatError("invalid start-tick baseline %r for pid %d" % (start, pid))
    fields = _stat_fields(pid)
    if fields is None:
        return None
    if fields[19] != start:
        return None
    return fields[0] not in (b"Z", b"X")


def _wait(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _wait_file(path, timeout):
    return _wait(lambda: path.exists() and path.read_text().strip() != "", timeout)


def _stopped(pid, start):
    return lambda: _running(pid, start) is not True


def _fake_stat(state=b"R", start=b"12345", length=20):
    """A synthetic post-``comm`` stat with a chosen state byte and start tick."""
    fields = [b"0"] * length
    fields[0] = state
    fields[19] = start
    return b"4242 (fake) " + b" ".join(fields)


# --- controlled fake processes (no native Codex, no model, no network) --------

# A TERM-ignoring grandchild that ACKNOWLEDGES readiness through a FILE only
# after its SIGTERM handler is installed, so a resistant child is never recorded
# by the parent before it is actually resistant. Never writes to stdout (that is
# the native RPC stream).
GRANDCHILD = '''
import signal,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1],"w").write("ready")
time.sleep(600)
'''

# Native that ignores TERM and leaves a TERM-ignoring grandchild behind. It
# waits for the grandchild's readiness ack BEFORE writing the fixture pid record.
NATIVE_IGNORE = '''
import os,signal,subprocess,sys,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
ready=sys.argv[2]
g=subprocess.Popen([sys.executable,"-c",__G__,ready])
while not os.path.exists(ready): time.sleep(0.01)
open(sys.argv[1],"w").write("%d %d"%(os.getpid(),g.pid))
while True: time.sleep(1)
'''.replace("__G__", repr(GRANDCHILD))

# Native that exits at once, leaving a TERM-ignoring grandchild behind.
NATIVE_EARLY_EXIT = '''
import os,subprocess,sys,time
ready=sys.argv[2]
g=subprocess.Popen([sys.executable,"-c",__G__,ready])
while not os.path.exists(ready): time.sleep(0.01)
open(sys.argv[1],"w").write("%d %d"%(os.getpid(),g.pid))
'''.replace("__G__", repr(GRANDCHILD))

# Native that records its pid and idles (honours TERM); no grandchild.
NATIVE_IDLE = '''
import os,sys,time
open(sys.argv[1],"w").write("%d"%os.getpid())
while True: time.sleep(1)
'''

# Native that answers exactly one JSON-RPC line then idles with a grandchild.
NATIVE_RPC = '''
import json,os,subprocess,sys,time
sys.stdout.write(json.dumps({"ok":True})+"\\n");sys.stdout.flush()
ready=sys.argv[2]
g=subprocess.Popen([sys.executable,"-c",__G__,ready])
while not os.path.exists(ready): time.sleep(0.01)
open(sys.argv[1],"w").write("%d %d"%(os.getpid(),g.pid))
while True: time.sleep(1)
'''.replace("__G__", repr(GRANDCHILD))

# Native that exits immediately without ever writing to stdout.
NATIVE_SILENT = "import time\ntime.sleep(0.05)\n"

# Writes a marker file; used to prove the native was NEVER launched.
NATIVE_MARKER = '''
import sys
open(sys.argv[1],"w").write("spawned")
'''

# Python "parent": spawns the guardian detached (own group+session), records the
# guardian pid AND its start tick (read while the guardian is certainly alive),
# then waits to be hard-killed.
BOSS = '''
import os,subprocess,sys,time
def tick(pid):
    b=open("/proc/%d/stat"%pid,"rb").read()
    return b[b.rfind(b")")+1:].split()[19].decode()
guardian,native,deadline,boss_ready,gpidfile,pidfile,grand_ready=sys.argv[1:8]
proc=subprocess.Popen([sys.executable,guardian,"--parent-pid",str(os.getpid()),
    "--deadline",deadline,"--",sys.executable,native,pidfile,grand_ready],start_new_session=True)
open(gpidfile,"w").write("%d %s"%(proc.pid,tick(proc.pid)))
open(boss_ready,"w").write("%d"%os.getpid())
time.sleep(120)
'''

# Python "parent": spawns the guardian detached, records its pid+ tick, then dies.
BOSS_QUICK = '''
import os,subprocess,sys
def tick(pid):
    b=open("/proc/%d/stat"%pid,"rb").read()
    return b[b.rfind(b")")+1:].split()[19].decode()
guardian,native,deadline,gpidfile,pidfile=sys.argv[1:6]
proc=subprocess.Popen([sys.executable,guardian,"--parent-pid",str(os.getpid()),
    "--deadline",deadline,"--",sys.executable,native,pidfile],start_new_session=True)
open(gpidfile,"w").write("%d %s"%(proc.pid,tick(proc.pid)))
os._exit(0)
'''


class StatHelperTests(unittest.TestCase):
    """Unit regressions for the pid-identity helpers (fault injection)."""

    def _fault(self, exc):
        return patch.object(THIS, "_read_stat_bytes", side_effect=exc)

    def test_only_enoent_and_esrch_mean_gone(self):
        for exc in (FileNotFoundError(errno.ENOENT, "gone"),
                    ProcessLookupError(errno.ESRCH, "gone")):
            with self.subTest(exc=type(exc).__name__), self._fault(exc):
                self.assertIsNone(_stat_fields(4242))
                self.assertIsNone(_running(4242, b"99"))
                self.assertTrue(_stopped(4242, b"99")())

    def test_permission_error_fails_the_proof(self):
        with self._fault(PermissionError(errno.EACCES, "denied")):
            self.assertRaises(StatError, _stat_fields, 4242)
            self.assertRaises(StatError, _running, 4242, b"99")
            self.assertRaises(StatError, _stopped(4242, b"99"))

    def test_io_error_fails_the_proof(self):
        with self._fault(OSError(errno.EIO, "input/output error")):
            self.assertRaises(StatError, _stat_fields, 4242)
            self.assertRaises(StatError, _identity, 4242)

    def test_malformed_stat_fails_the_proof(self):
        with patch.object(THIS, "_read_stat_bytes", return_value=b"garbage with no comm terminator"):
            self.assertRaises(StatError, _stat_fields, 4242)
            self.assertRaises(StatError, _running, 4242, b"99")

    def test_short_stat_fails_the_proof(self):
        with patch.object(THIS, "_read_stat_bytes", return_value=b"7 (x) R 1 2 3"):
            self.assertRaises(StatError, _stat_fields, 4242)

    def test_full_length_invalid_observed_start_tick_fails_the_proof(self):
        with patch.object(THIS, "_read_stat_bytes", return_value=_fake_stat(start=b"not-a-start-tick")):
            self.assertRaises(StatError, _stat_fields, 4242)
            self.assertRaises(StatError, _identity, 4242)
            self.assertRaises(StatError, _running, 4242, b"12345")
            self.assertRaises(StatError, _stopped(4242, b"12345"))

    def test_full_length_invalid_observed_state_fails_the_proof(self):
        with patch.object(THIS, "_read_stat_bytes", return_value=_fake_stat(state=b"unknown")):
            self.assertRaises(StatError, _stat_fields, 4242)
            self.assertRaises(StatError, _running, 4242, b"12345")
            self.assertRaises(StatError, _stopped(4242, b"12345"))

    def test_valid_record_live_zombie_x_and_recycled(self):
        with patch.object(THIS, "_read_stat_bytes", return_value=_fake_stat(b"R", b"555")):
            self.assertIs(_running(1, b"555"), True)
            self.assertIs(_stopped(1, b"555")(), False)
        with patch.object(THIS, "_read_stat_bytes", return_value=_fake_stat(b"Z", b"555")):
            self.assertIs(_running(1, b"555"), False)
            self.assertTrue(_stopped(1, b"555")())
        with patch.object(THIS, "_read_stat_bytes", return_value=_fake_stat(b"X", b"555")):
            self.assertIs(_running(1, b"555"), False)
        # A recycled pid parses fine but carries a DIFFERENT identity -> gone.
        with patch.object(THIS, "_read_stat_bytes", return_value=_fake_stat(b"R", b"999")):
            self.assertIsNone(_running(1, b"555"))
            self.assertTrue(_stopped(1, b"555")())

    def test_invalid_baseline_identity_is_refused(self):
        with patch.object(THIS, "_read_stat_bytes", return_value=_fake_stat(b"R", b"555")):
            self.assertRaises(StatError, _running, 1, None)
            self.assertRaises(StatError, _running, 1, b"")
            self.assertRaises(StatError, _running, 1, b"not-a-tick")
            self.assertRaises(StatError, _stopped(1, None))

    def test_real_reaped_child_is_gone(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        start = _identity(proc.pid)  # present (running or zombie) -> valid record
        proc.wait()
        self.assertIsNone(_stat_fields(proc.pid))
        self.assertIsNone(_running(proc.pid, start))
        self.assertTrue(_stopped(proc.pid, start)())

    def test_real_zombie_is_not_running(self):
        pid = os.fork()
        if pid == 0:  # child: become an unreaped zombie
            os._exit(0)
        try:
            self.assertTrue(_wait(lambda: _state(pid) == b"Z", 2), "child never zombied")
            start = _identity(pid)
            self.assertIs(_running(pid, start), False)
            self.assertTrue(_stopped(pid, start)())
        finally:
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tracked = []
        self.groups = []
        # In-process main() tests share these module flags; start each clean.
        guardian._TERMINATED = False
        guardian._CLEANING = False

    def tearDown(self):
        for pid, start in self.groups:
            if self._same_leader(pid, start):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
        for pid, start in self.tracked:
            if _running(pid, start) is True:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        _wait(lambda: all(_running(pid, start) is not True for pid, start in self.tracked), 2.0)
        self.temp.cleanup()

    @staticmethod
    def _same_leader(pid, start):
        try:
            fields = _stat_fields(pid)
        except AssertionError:
            return False
        return fields is not None and fields[19] == start

    def _write(self, name, source):
        path = self.root / name
        path.write_text(source)
        return path

    def _track(self, pid, start=None):
        self.tracked.append((pid, start if start is not None else _identity(pid)))

    def _own_group(self, pid, start=None):
        self.groups.append((pid, start if start is not None else _identity(pid)))

    def _spawn_guardian(self, native_name, native_source, pidfile_name, deadline, grand):
        native = self._write(native_name, native_source)
        pidfile = self.root / pidfile_name
        native_argv = [sys.executable, str(native), str(pidfile)]
        if grand:
            native_argv.append(str(self.root / (pidfile_name + ".grand")))
        proc = subprocess.Popen(
            [sys.executable, GUARDIAN, "--parent-pid", str(os.getpid()),
             "--deadline", repr(float(deadline)), "--", *native_argv],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._own_group(proc.pid)
        self._track(proc.pid)
        return proc, pidfile

    # --- hard parent death ----------------------------------------------------

    def test_hard_parent_death_reclaims_native_and_grandchild(self):
        native = self._write("native_ignore.py", NATIVE_IGNORE)
        boss = self._write("boss.py", BOSS)
        boss_ready, gpidfile, pidfile = self.root / "ready", self.root / "gpid", self.root / "pids"
        grand_ready = self.root / "pids.grand"
        deadline = repr(time.monotonic() + 30)
        boss_proc = subprocess.Popen(
            [sys.executable, str(boss), GUARDIAN, str(native), deadline,
             str(boss_ready), str(gpidfile), str(pidfile), str(grand_ready)])
        self._track(boss_proc.pid)
        self.assertTrue(_wait_file(pidfile, 5), "native never became ready")
        gpid_s, gstart_s = gpidfile.read_text().split()
        gpid, gstart = int(gpid_s), gstart_s.encode()
        self._track(gpid, gstart)
        self._own_group(gpid, gstart)
        native_pid, grand_pid = (int(x) for x in pidfile.read_text().split())
        nstart, gstartc = _identity(native_pid), _identity(grand_pid)
        self._track(native_pid, nstart)
        self._track(grand_pid, gstartc)
        # Evidence the guardian is the session leader and owns the native group.
        self.assertEqual(_session(gpid), str(gpid).encode())
        self.assertEqual(_pgrp(native_pid), str(gpid).encode())
        self.assertIs(_running(native_pid, nstart), True)
        # Hard-kill the Python parent only; the detached guardian must reclaim.
        os.kill(boss_proc.pid, signal.SIGKILL)
        boss_proc.wait()
        self.assertTrue(_wait(_stopped(native_pid, nstart), 3),
                        "native survived hard parent death")
        self.assertTrue(_wait(_stopped(grand_pid, gstartc), 3),
                        "grandchild survived hard parent death")

    def test_parent_death_at_startup_refuses_or_reclaims(self):
        native = self._write("native_idle.py", NATIVE_IDLE)
        quick = self._write("boss_quick.py", BOSS_QUICK)
        gpidfile, pidfile = self.root / "gpid", self.root / "pids"
        launcher = subprocess.Popen(
            [sys.executable, str(quick), GUARDIAN, str(native), repr(time.monotonic() + 30),
             str(gpidfile), str(pidfile)], start_new_session=True)
        launcher.wait(timeout=10)
        self.assertTrue(_wait_file(gpidfile, 5), "guardian pid was never recorded")
        gpid_s, gstart_s = gpidfile.read_text().split()
        gpid, gstart = int(gpid_s), gstart_s.encode()
        self._track(gpid, gstart)
        self._own_group(gpid, gstart)
        self.assertTrue(_wait(_stopped(gpid, gstart), 5),
                        "guardian persisted after startup parent death")
        if pidfile.exists() and pidfile.read_text().strip():
            npid = int(pidfile.read_text().split()[0])
            nstart = _identity_if_recorded(npid)
            if nstart is not None:
                self._track(npid, nstart)
                self.assertTrue(_wait(_stopped(npid, nstart), 3),
                                "native persisted after startup parent death")

    # --- signals and deadlines ------------------------------------------------

    def test_repeated_term_is_ignored_and_grace_stays_fixed(self):
        proc, pidfile = self._spawn_guardian("native_ignore.py", NATIVE_IGNORE, "pids",
                                             time.monotonic() + 30, grand=True)
        self.assertTrue(_wait_file(pidfile, 5), "native never became ready")
        native_pid, grand_pid = (int(x) for x in pidfile.read_text().split())
        nstart, gstart = _identity(native_pid), _identity(grand_pid)
        self._track(native_pid, nstart)
        self._track(grand_pid, gstart)
        start = time.monotonic()
        for _ in range(6):
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            time.sleep(0.02)
        proc.wait(timeout=6)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, "repeated TERM extended the fixed grace")
        self.assertTrue(_wait(_stopped(native_pid, nstart), 3))
        self.assertTrue(_wait(_stopped(grand_pid, gstart), 3))
        out, _err = proc.communicate()
        self.assertEqual(out, b"")  # stdout belongs to the RPC stream, never us

    def test_original_deadline_reclaims_the_group(self):
        start = time.monotonic()
        proc, pidfile = self._spawn_guardian("native_idle.py", NATIVE_IDLE, "pids",
                                             start + 0.5, grand=False)
        self.assertTrue(_wait_file(pidfile, 5), "native never became ready")
        npid = int(pidfile.read_text().split()[0])
        nstart = _identity(npid)
        self._track(npid, nstart)
        proc.communicate(timeout=6)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.35, "guardian reclaimed before the deadline")
        self.assertLess(elapsed, 3.0)
        self.assertTrue(_wait(_stopped(npid, nstart), 3))

    def test_native_early_exit_reclaims_the_grandchild(self):
        proc, pidfile = self._spawn_guardian("native_early.py", NATIVE_EARLY_EXIT, "pids",
                                             time.monotonic() + 30, grand=True)
        self.assertTrue(_wait_file(pidfile, 5), "native never started")
        _native_pid, grand_pid = (int(x) for x in pidfile.read_text().split())
        gstart = _identity(grand_pid)
        self._track(grand_pid, gstart)
        proc.wait(timeout=6)
        self.assertTrue(_wait(_stopped(grand_pid, gstart), 3),
                        "grandchild survived native early exit")
        out, _err = proc.communicate()
        self.assertEqual(out, b"")

    # --- normal close and RPC semantics ---------------------------------------

    def test_normal_rpcclient_close_keeps_a_verified_result(self):
        native = self._write("native_rpc.py", NATIVE_RPC)
        pidfile = self.root / "pids"
        grand_ready = self.root / "pids.grand"
        command = generate.guardian_command(
            [sys.executable, str(native), str(pidfile), str(grand_ready)], time.monotonic() + 30)
        client = generate.bridge.RpcClient(command, 30)
        self._own_group(client.process.pid)
        self._track(client.process.pid)
        row = client.next()  # the verified result read from the native stream
        self.assertEqual(row, {"ok": True})
        self.assertTrue(_wait_file(pidfile, 5), "native never became ready")
        native_pid, grand_pid = (int(x) for x in pidfile.read_text().split())
        nstart, gstart = _identity(native_pid), _identity(grand_pid)
        self._track(native_pid, nstart)
        self._track(grand_pid, gstart)
        client.close(interrupt=False)  # RpcClient group-kills guardian + native
        self.assertTrue(_wait(_stopped(native_pid, nstart), 3))
        self.assertTrue(_wait(_stopped(grand_pid, gstart), 3))
        self.assertEqual(row, {"ok": True})  # result survives normal cleanup

    def test_guardian_exit_never_establishes_rpc_success(self):
        native = self._write("native_silent.py", NATIVE_SILENT)
        command = generate.guardian_command([sys.executable, str(native)], time.monotonic() + 5)
        client = generate.bridge.RpcClient(command, 5)
        self._own_group(client.process.pid)
        self._track(client.process.pid)
        with self.assertRaises(generate.bridge.BridgeError) as caught:
            client.next()  # native exited with no frame -> EOF, never success
        self.assertEqual(caught.exception.code, "rpc_eof")
        client.close(interrupt=False)

    # --- refusals -------------------------------------------------------------

    def test_standalone_invocation_without_own_group_is_refused(self):
        native = self._write("native_marker.py", NATIVE_MARKER)
        marker = self.root / "spawned"
        # NO start_new_session: the guardian shares THIS process's group, so it
        # must refuse before launching anything rather than kill our group.
        proc = subprocess.Popen(
            [sys.executable, GUARDIAN, "--parent-pid", str(os.getpid()),
             "--deadline", repr(time.monotonic() + 30), "--", sys.executable, str(native), str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.wait(timeout=5)
        out, _err = proc.communicate()
        self.assertEqual(proc.returncode, guardian.EXIT_GROUP)
        self.assertEqual(out, b"")
        self.assertFalse(marker.exists(), "native ran despite the refusal")

    def test_invalid_parent_and_expired_deadline_refuse_before_launch(self):
        native = self._write("native_marker.py", NATIVE_MARKER)
        marker = self.root / "spawned"
        for bad_parent in ("1", "0", "-4"):
            with self.subTest(parent=bad_parent):
                proc = subprocess.Popen(
                    [sys.executable, GUARDIAN, "--parent-pid", bad_parent,
                     "--deadline", repr(time.monotonic() + 30), "--", sys.executable, str(native), str(marker)],
                    start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                proc.wait(timeout=5)
                out, _err = proc.communicate()
                self.assertEqual(proc.returncode, guardian.EXIT_PARENT)
                self.assertEqual(out, b"")
        proc = subprocess.Popen(
            [sys.executable, GUARDIAN, "--parent-pid", str(os.getpid()),
             "--deadline", repr(time.monotonic() - 1), "--", sys.executable, str(native), str(marker)],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, guardian.EXIT_DEADLINE)
        proc = subprocess.Popen([sys.executable, GUARDIAN, "bogus"],
                                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, guardian.EXIT_USAGE)
        self.assertFalse(marker.exists(), "native ran despite the refusals")

    def test_platform_prctl_and_self_parent_refusals_never_launch(self):
        native = self._write("native_marker.py", NATIVE_MARKER)
        marker = self.root / "spawned"
        native_argv = [sys.executable, str(native), str(marker)]
        with patch.object(guardian, "reclaim"), patch.object(guardian, "_signal_group"):
            with patch.object(guardian.sys, "platform", "darwin"):
                code = guardian.main(["--parent-pid", str(os.getppid()),
                                      "--deadline", repr(time.monotonic() + 30), "--", *native_argv])
            self.assertEqual(code, guardian.EXIT_PLATFORM)
            # expected parent == this very process (currentPID) is refused
            code = guardian.main(["--parent-pid", str(os.getpid()),
                                  "--deadline", repr(time.monotonic() + 30), "--", *native_argv])
            self.assertEqual(code, guardian.EXIT_PARENT)
            # prctl failure: owns the group, but PR_SET_PDEATHSIG did not succeed
            original = signal.getsignal(signal.SIGTERM)
            try:
                with patch.object(guardian, "_owns_group", return_value=True), patch.object(
                        guardian, "set_pdeathsig", return_value=False):
                    code = guardian.main(["--parent-pid", str(os.getppid()),
                                          "--deadline", repr(time.monotonic() + 30), "--", *native_argv])
            finally:
                signal.signal(signal.SIGTERM, original)
        self.assertEqual(code, guardian.EXIT_PRCTL)
        self.assertFalse(marker.exists(), "native ran despite the refusals")

    # --- refusal windows around set_pdeathsig / launch (destructive calls mocked)

    def test_term_during_pdeathsig_window_never_launches(self):
        native = self._write("native_marker.py", NATIVE_MARKER)
        marker = self.root / "spawned"
        argv = ["--parent-pid", str(os.getppid()), "--deadline", repr(time.monotonic() + 30),
                "--", sys.executable, str(native), str(marker)]

        def term_mid_prctl(signum=signal.SIGTERM):
            guardian._on_term(signal.SIGTERM, None)  # TERM arrives DURING the prctl window
            return True

        original = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(guardian, "_owns_group", return_value=True), \
                 patch.object(guardian, "set_pdeathsig", side_effect=term_mid_prctl), \
                 patch.object(guardian, "_signal_group") as kill, \
                 patch.object(guardian, "reclaim") as reclaim, \
                 patch.object(guardian.subprocess, "Popen") as popen:
                code = guardian.main(argv)
        finally:
            guardian._TERMINATED = False
            signal.signal(signal.SIGTERM, original)
        self.assertEqual(code, guardian.EXIT_SIGNALLED)
        popen.assert_not_called()
        reclaim.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(marker.exists(), "native ran despite a TERM during the prctl window")

    def test_deadline_during_pdeathsig_window_never_launches(self):
        native = self._write("native_marker.py", NATIVE_MARKER)
        marker = self.root / "spawned"
        clock = {"t": 1_000_000.0}
        deadline = clock["t"] + 30.0

        def expire_mid_prctl(signum=signal.SIGTERM):
            clock["t"] = deadline + 1.0  # the shared deadline passes DURING the prctl window
            return True

        original = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(guardian, "_owns_group", return_value=True), \
                 patch.object(guardian, "set_pdeathsig", side_effect=expire_mid_prctl), \
                 patch.object(guardian.time, "monotonic", side_effect=lambda: clock["t"]), \
                 patch.object(guardian, "_signal_group") as kill, \
                 patch.object(guardian, "reclaim") as reclaim, \
                 patch.object(guardian.subprocess, "Popen") as popen:
                code = guardian.main(["--parent-pid", str(os.getppid()), "--deadline", repr(deadline),
                                      "--", sys.executable, str(native), str(marker)])
        finally:
            signal.signal(signal.SIGTERM, original)
        self.assertEqual(code, guardian.EXIT_DEADLINE)
        popen.assert_not_called()
        reclaim.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(marker.exists(), "native ran despite a deadline during the prctl window")

    def test_refusal_after_launch_reclaims_the_new_group(self):
        native = self._write("native_marker.py", NATIVE_MARKER)
        marker = self.root / "spawned"
        sentinel = object()
        refusals = iter([None, guardian.EXIT_PARENT])  # pass pre-spawn, refuse post-spawn

        def fake_refuse(parent, deadline):
            return next(refusals)

        original = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(guardian, "_owns_group", return_value=True), \
                 patch.object(guardian, "set_pdeathsig", return_value=True), \
                 patch.object(guardian, "_refuse", side_effect=fake_refuse), \
                 patch.object(guardian.subprocess, "Popen", return_value=sentinel) as popen, \
                 patch.object(guardian, "reclaim") as reclaim, \
                 patch.object(guardian, "_monitor") as monitor:
                code = guardian.main(["--parent-pid", str(os.getppid()),
                                      "--deadline", repr(time.monotonic() + 30), "--",
                                      sys.executable, str(native), str(marker)])
        finally:
            signal.signal(signal.SIGTERM, original)
        self.assertEqual(code, guardian.EXIT_PARENT)
        popen.assert_called_once()
        reclaim.assert_called_once_with(sentinel)
        monitor.assert_not_called()
        self.assertFalse(marker.exists())

    def test_reclaim_terms_then_kills_without_touching_the_runner_group(self):
        signals = []
        fake_native = types.SimpleNamespace(poll=lambda: None)
        original = signal.getsignal(signal.SIGTERM)
        try:
            guardian._CLEANING = False
            with patch.object(guardian, "_signal_group", side_effect=signals.append), \
                 patch.object(guardian, "_running_group_members", return_value=[]), \
                 patch.object(guardian, "CLEANUP_GRACE", 0.0), \
                 patch.object(guardian.os, "_exit") as hard_exit:
                guardian.reclaim(fake_native)
        finally:
            guardian._CLEANING = False
            signal.signal(signal.SIGTERM, original)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        hard_exit.assert_called_once_with(guardian.EXIT_CLEANUP_FAILED)


if __name__ == "__main__":
    unittest.main()
