import datetime as dt
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('coding_window', ROOT / 'scripts/coding-window.py')
window = importlib.util.module_from_spec(spec); spec.loader.exec_module(window)

# Real leader/grandchild pair. The grandchild ignores SIGTERM so only a bounded
# group-level SIGKILL can clear it. The leader writes "<leader> <grandchild>".
LEADER_EXITS = (
    "import os,signal,sys,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "grand = os.fork()\n"
    "if grand == 0:\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    time.sleep(120)\n"
    "    os._exit(0)\n"
    "with open(sys.argv[1], 'w') as fh:\n"
    "    fh.write('%d %d' % (os.getpid(), grand))\n"
    "os._exit(7)\n"
)
LEADER_SURVIVES = (
    "import os,signal,sys,time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "grand = os.fork()\n"
    "if grand == 0:\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    time.sleep(120)\n"
    "    os._exit(0)\n"
    "with open(sys.argv[1], 'w') as fh:\n"
    "    fh.write('%d %d' % (os.getpid(), grand))\n"
    "time.sleep(120)\n"
    "os._exit(0)\n"
)
SIGTERM_RUNNER = (
    "import importlib.util,sys,datetime\n"
    "spec = importlib.util.spec_from_file_location('cw', sys.argv[1])\n"
    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    "m.require_allowed = lambda: {'stop_at': (datetime.datetime.now(datetime.timezone.utc)"
    " + datetime.timedelta(minutes=10)).isoformat()}\n"
    "sys.exit(m.supervise(sys.argv[2:]))\n"
)


def _running(pid):
    try:
        with open('/proc/%d/stat' % pid, 'rb') as fh:
            stat = fh.read()
    except OSError:
        return False
    close = stat.rfind(b')')
    return close >= 0 and stat[close + 2:close + 3] not in (b'Z', b'X')


def _read_pids(path, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        try:
            text = Path(path).read_text().split()
            if len(text) == 2:
                return int(text[0]), int(text[1])
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError('leader pidfile was never written')


def _wait_dead(pid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not _running(pid):
            return
        time.sleep(0.05)
    raise AssertionError('process %d still alive' % pid)


def _force_kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass



class WindowTests(unittest.TestCase):
    def at(self, value): return window.decision(dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc))

    def test_each_weekday_edge_is_exact(self):
        for date in ['2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16']:
            for time, allowed in [('00:29:59',True),('00:30:00',False),('04:04:59',False),('04:05:00',True),('05:29:59',True),('05:30:00',False),('10:04:59',False),('10:05:00',True),('23:59:59',True)]:
                with self.subTest(date=date,time=time): self.assertEqual(self.at(date+'T'+time)['allowed'],allowed)

    def test_continuous_weekend_has_no_daily_artificial_boundary(self):
        for date in ['2026-10-17','2026-10-18']:
            for hour in range(24): self.assertTrue(self.at(f'{date}T{hour:02}:00:00')['allowed'])
        self.assertEqual(self.at('2026-10-16T10:05:00')['stop_at'],'2026-10-19T00:30:00+00:00')

    def test_next_start_and_utc_conversion(self):
        self.assertEqual(self.at('2026-10-13T02:00:00')['next_allowed_at'],'2026-10-13T04:05:00+00:00')
        self.assertEqual(self.at('2026-10-13T06:00:00')['next_allowed_at'],'2026-10-13T10:05:00+00:00')
        self.assertFalse(window.decision(dt.datetime.fromisoformat('2026-10-13T12:00:00+08:00'))['allowed'])
        with self.assertRaises(ValueError): window.decision(dt.datetime(2026,10,13))

    def test_verified_chinese_holidays_are_continuous_off_peak(self):
        self.assertTrue(self.at('2026-10-01T01:30:00')['allowed'])
        self.assertEqual(self.at('2026-09-30T10:05:00')['stop_at'],'2026-10-08T00:30:00+00:00')
        self.assertTrue(self.at('2026-10-10T02:00:00')['allowed'])

    def test_spring_festival_gap_reports_future_deadline(self):
        # Saturday 2026-02-14 and the 02-15..02-23 holiday leave no upcoming
        # window until Tuesday 02-24; allowed must carry that verified cutoff.
        for stamp in ('2026-02-14T12:00:00', '2026-02-15T01:30:00',
                      '2026-02-15T06:00:00', '2026-02-23T09:00:00'):
            with self.subTest(stamp=stamp):
                value = self.at(stamp)
                self.assertTrue(value['allowed'])
                self.assertEqual(value['stop_at'], '2026-02-24T00:30:00+00:00')

    def test_gap_longer_than_lookahead_fails_closed(self):
        stretch = {dt.date(2026, 2, 1) + dt.timedelta(days=i) for i in range(30)}
        with mock.patch.object(window, 'HOLIDAYS', stretch):
            value = self.at('2026-02-05T12:00:00')
        self.assertFalse(value['allowed'])
        self.assertIsNone(value['stop_at'])

    def test_supervise_refuses_to_launch_without_valid_cutoff(self):
        for bad in ({'stop_at': None}, {'stop_at': 'not-a-date'}, {}):
            with self.subTest(bad=bad):
                with mock.patch.object(window, 'require_allowed', lambda w=bad: w):
                    with mock.patch.object(window.subprocess, 'Popen') as popen:
                        with self.assertRaises(ValueError):
                            window.supervise(['/bin/true'])
                        popen.assert_not_called()

    def test_leader_exit_reaps_ignoring_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, 'pids')
            argv = [sys.executable, '-c', LEADER_EXITS, pidfile]
            window_value = {'stop_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()}
            with mock.patch.object(window, 'require_allowed', lambda: window_value):
                code = window.supervise(argv)
            self.assertEqual(code, 7)
            leader, grand = _read_pids(pidfile)
            try:
                _wait_dead(grand)
                _wait_dead(leader)
            finally:
                _force_kill(grand); _force_kill(leader)

    def test_deadline_kills_leader_and_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, 'pids')
            argv = [sys.executable, '-c', LEADER_SURVIVES, pidfile]
            # Cleanup starts five seconds before the cutoff, so give the leader
            # enough lead time to install SIG_IGN and record its pids first.
            stop = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=8)).isoformat()
            with mock.patch.object(window, 'require_allowed', lambda: {'stop_at': stop}):
                started = time.time()
                code = window.supervise(argv)
            self.assertEqual(code, 75)
            self.assertLess(time.time() - started, 20)
            leader, grand = _read_pids(pidfile)
            try:
                _wait_dead(leader)
                _wait_dead(grand)
            finally:
                _force_kill(leader); _force_kill(grand)

    def test_supervisor_sigterm_stops_group_and_returns_75(self):
        script = str(ROOT / 'scripts/coding-window.py')
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, 'pids')
            argv = [sys.executable, '-c', SIGTERM_RUNNER, script,
                    sys.executable, '-c', LEADER_SURVIVES, pidfile]
            proc = subprocess.Popen(argv)
            try:
                leader, grand = _read_pids(pidfile)
                proc.send_signal(signal.SIGTERM)
                code = proc.wait(timeout=25)
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait()
            self.assertEqual(code, 75)
            try:
                _wait_dead(leader)
                _wait_dead(grand)
            finally:
                _force_kill(leader); _force_kill(grand)


if __name__=='__main__': unittest.main()
