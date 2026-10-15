#!/usr/bin/env python3
"""UTC coding gate: DeepSeek off-peak, +5 minute start / -30 minute stop."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

UTC = dt.timezone.utc
SOURCE = 'https://api-docs.deepseek.com/quick_start/pricing/'
VERIFIED = '2026-10-13'
# Days of forward lookahead for the next peak-window start. The verified 2026
# calendar's longest off-peak stretch (Spring Festival / weekend, 2026-02-14 ..
# 2026-02-23) needs +11 days from the last working day (2026-02-13) to reach the
# next cutoff (2026-02-24). 14 leaves bounded margin and still fails closed.
LOOKAHEAD_DAYS = 14
HOLIDAY_SOURCE = 'https://www.beijing.gov.cn/fuwu/bmfw/sy/jrts/202511/t20251104_4258838.html'
HOLIDAYS = set()
for first, last in [('2026-01-01','2026-01-03'),('2026-02-15','2026-02-23'),('2026-04-04','2026-04-06'),
                    ('2026-05-01','2026-05-05'),('2026-06-19','2026-06-21'),('2026-09-25','2026-09-27'),('2026-10-01','2026-10-07')]:
    day, end = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    while day <= end:
        HOLIDAYS.add(day); day += dt.timedelta(days=1)


def decision(now=None):
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError('UTC-aware time is required')
    now = now.astimezone(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    blocked = []
    for offset in range(-1, LOOKAHEAD_DAYS):
        date = day + dt.timedelta(days=offset)
        if date.weekday() < 5 and date.date() not in HOLIDAYS:
            for start, end in ((1, 4), (6, 10)):
                blocked.append((date + dt.timedelta(hours=start, minutes=-30), date + dt.timedelta(hours=end, minutes=5)))
    current = next(((start, end) for start, end in blocked if start <= now < end), None)
    cutoff = next((start for start, end in blocked if start > now), None)
    # Fail closed: never report an open window without a verified future cutoff,
    # so a caller can never be handed stop_at=None and launch unbounded.
    allowed = current is None and cutoff is not None
    reason = ('peak_or_buffer' if current is not None else
              'off_peak_buffered' if cutoff is not None else 'no_cutoff')
    return {'allowed': allowed, 'timezone': 'UTC', 'observed_at': now.isoformat(),
            'next_allowed_at': (current[1] if current else now).isoformat(),
            'stop_at': cutoff.isoformat() if allowed else None,
            'reason': reason,
            'source': SOURCE, 'verified_at': VERIFIED, 'holiday_source': HOLIDAY_SOURCE,
            'holiday_calendar_years': [2026], 'unknown_year_policy': 'conservative_weekday_peak',
            'start_delay_minutes': 5, 'stop_early_minutes': 30}


def require_allowed():
    value = decision()
    if not value['allowed']:
        raise ValueError('Coding paused for DeepSeek peak/buffer window; inspect coding-window.py status')
    return value


def _group_alive(pgid):
    """True while a live (non-zombie) process remains in process group pgid."""
    try:
        entries = os.listdir('/proc')
    except OSError:
        return False
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % name, 'rb') as handle:
                stat = handle.read()
        except OSError:
            continue
        close = stat.rfind(b')')
        if close < 0:
            continue
        fields = stat[close + 2:].split()
        if len(fields) < 3 or fields[0] == b'Z':
            continue
        try:
            if int(fields[2]) == pgid:
                return True
        except ValueError:
            continue
    return False


def _terminate_group(pgid, deadline):
    """TERM the group, wait until deadline, then KILL; ignore an empty group."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    while time.time() < deadline and _group_alive(pgid):
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def supervise(argv):
    window = require_allowed()
    stop_at = window.get('stop_at')
    if not stop_at:
        raise ValueError('coding window has no verified cutoff; refusing to launch')
    try:
        cutoff = dt.datetime.fromisoformat(stop_at).timestamp()
    except (TypeError, ValueError):
        raise ValueError('coding window cutoff is invalid; refusing to launch')
    child = subprocess.Popen(argv, start_new_session=True)
    stopped = False

    def stop(sig, frame):
        nonlocal stopped
        stopped = True

    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        halted = False
        while child.poll() is None:
            # Allow cleanup five seconds BEFORE the deadline. Force at the
            # deadline; no coding process intentionally runs into the buffer.
            if stopped or time.time() >= cutoff - 5:
                halted = True
                break
            time.sleep(0.2)
        # The leader may exit while descendants in its own session/group keep
        # running. Always TERM then bounded KILL that group so nothing outlives
        # the window; only our child's group is ever signalled, never others.
        deadline = min(time.time() + 3, cutoff)
        _terminate_group(child.pid, deadline)
        child.wait()
        if halted:
            return 75
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        _terminate_group(child.pid, min(time.time() + 3, cutoff))
        child.wait()
        for s, handler in old.items(): signal.signal(s, handler)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['status', 'check', 'run'])
    p.add_argument('argv', nargs=argparse.REMAINDER)
    a = p.parse_args()
    if a.command in ('status', 'check'):
        value = decision(); print(json.dumps(value))
        return 0 if a.command == 'status' or value['allowed'] else 75
    argv = a.argv[1:] if a.argv[:1] == ['--'] else a.argv
    if not argv: p.error('run requires a command')
    try: return supervise(argv)
    except (ValueError, OSError):
        print('Coding dispatch unavailable; inspect off-peak schedule and private prerequisites.', file=sys.stderr)
        return 75


if __name__ == '__main__': raise SystemExit(main())
