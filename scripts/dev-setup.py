#!/usr/bin/env python3
"""Generate new local development secrets without logging or overwriting them."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
path = root / ".env"
if path.exists() or path.is_symlink():
    raise SystemExit("Existing .env preserved. Use the existing development configuration.")
keys = ("MARIADB_ROOT_PASSWORD", "CMS_DB_PASSWORD", "CMS_MIGRATION_DB_PASSWORD", "POLARIS_DB_PASSWORD", "REDIS_PASSWORD")
content = "# Private local development configuration\n" + "".join(f"{key}={secrets.token_hex(24)}\n" for key in keys)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(content)
print("Private development configuration created. Start with docker compose up --build -d.")
