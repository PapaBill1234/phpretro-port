#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Tiny runtime-lock helper: holds an OS advisory (flock) lock on a stable file.

The Node host spawns this once, holding our stdin pipe open for the runtime
lifetime. We acquire an exclusive non-blocking flock on the lock inode, report
LOCKED, then block until stdin reaches EOF (the host exited or released), and
finally drop the lock. Because the lock lives on the open file description, a
crashed host closes the pipe and we release automatically, while the file itself
is never unlinked or trusted by PID. Any unsafe path (symlink, non-regular,
foreign owner, loose mode) fails closed before the lock is taken.
"""

import errno
import fcntl
import os
import stat
import sys


def fail(message, code):
    sys.stderr.write(f"runtime-lock: {message}\n")
    sys.stderr.flush()
    sys.exit(code)


def main():
    if len(sys.argv) != 2:
        fail("expected exactly one lock path", 64)
    path = sys.argv[1]
    if not path or "\0" in path:
        fail("malformed lock path", 64)

    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        fail(f"cannot open lock path ({errno.errorcode.get(error.errno, error.errno)})", 66)

    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        fail("lock path is not a regular file", 65)
    if info.st_uid != os.getuid():
        fail("lock file is not owned by the runtime user", 65)
    if info.st_mode & 0o077:
        fail("lock file permissions must be 0600", 65)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            sys.stdout.write("BUSY\n")
            sys.stdout.flush()
            os.close(fd)
            sys.exit(3)
        fail(f"flock failed ({errno.errorcode.get(error.errno, error.errno)})", 69)

    sys.stdout.write("LOCKED\n")
    sys.stdout.flush()
    # Hold the lock until the host closes our stdin (normal release or crash).
    sys.stdin.buffer.read()
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    sys.exit(0)


if __name__ == "__main__":
    main()
