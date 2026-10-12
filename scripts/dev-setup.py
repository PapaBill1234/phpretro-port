#!/usr/bin/env python3
"""Generate new local development secrets without logging or overwriting them."""
import os
import argparse
from pathlib import Path
import re
import secrets
import stat
import tempfile

KEYS = ("MARIADB_ROOT_PASSWORD", "CMS_DB_PASSWORD", "CMS_MIGRATION_DB_PASSWORD", "POLARIS_DB_PASSWORD", "REDIS_PASSWORD", "CMS_POLARIS_DB_PASSWORD", "CMS_STAFF_ENCRYPTION_KEY")


def configure(root: Path, upgrade: bool = False) -> str:
    path = root / ".env"
    content = "# Private local development configuration\n"
    existing = {}
    if path.exists() or path.is_symlink():
        if not upgrade:
            raise ValueError("Existing .env preserved. Use --upgrade to add missing development keys.")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("Upgrade requires an owned regular mode-0600 .env.")
        content = path.read_text()
        for line in content.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
            if not match or match[1] in existing:
                raise ValueError("Existing .env has unsupported or duplicate entries; preserved unchanged.")
            existing[match[1]] = match[2]
        # Existing database/cache credentials must survive unchanged on initialized volumes.
        if any(not existing.get(key) for key in KEYS[:5]):
            raise ValueError("Existing base database/cache credentials are incomplete; preserved unchanged.")
    additions = ""
    for key in KEYS:
        if key not in existing:
            additions += f"{key}={secrets.token_hex(32 if key == 'CMS_STAFF_ENCRYPTION_KEY' else 24)}\n"
        elif not existing[key]:
            raise ValueError("Existing required credential is empty; preserved unchanged.")
    for key, value in (("LOCAL_UID", os.getuid()), ("LOCAL_GID", os.getgid())):
        if key not in existing:
            additions += f"{key}={value}\n"
    if not additions:
        return "Existing private development configuration is complete."
    content = content.rstrip("\n") + "\n" + additions
    if existing:
        fd, temporary = tempfile.mkstemp(prefix=".env-upgrade-", dir=root)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
    return "Private development configuration ready. Existing credentials preserved."


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="Append missing keys to an owned private .env without rotating existing credentials")
    args = parser.parse_args()
    try:
        print(configure(Path(__file__).resolve().parents[1], args.upgrade))
    except ValueError as error:
        raise SystemExit(str(error))
