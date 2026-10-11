# Start the development foundation

Requirements: Docker Engine with Compose, Python 3, and a free local port 8789. The stack uses project-owned containers and a new MariaDB volume; the previous preservation/runtime projects are not used as live services.

```bash
python3 scripts/dev-setup.py
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8789/api/v1/ready
```

Open `http://127.0.0.1:8789`. The setup script creates a mode-0600 ignored `.env` containing fresh development database/cache passwords. It preserves an existing file. Provider credentials are separate and are never passed to the CMS containers.

MariaDB and Redis have no published host ports. A one-shot migration service uses `cms_migrator`; the running CMS uses `cms_app` with read-only grants for this slice. The empty `polaris` database is reserved for the later emulator integration; this command does not install or start Polaris/Octane.

This first slice supplies health/readiness and persisted site settings, original CSS theme shells, client preview of the two themes, and English/Dutch presentation. Theme preview and language selection in this shell do not claim housekeeping configuration or a completed Habbo/PHPRetro parity implementation. No demo account is logged in and no pretend SSO ticket is issued.

Stopping without deleting the development data:

```bash
docker compose stop
```

Restarting:

```bash
docker compose up -d
```

New migration files belong to the CMS migrator. Do not change generated credentials in place and expect existing MariaDB users to be re-created: database initialization runs only on a new volume. Preserve the data and perform an explicit credential rotation if needed.

Builder sessions use a different launcher:

```bash
python3 scripts/claude-builder.py --role lead --check
python3 scripts/claude-builder.py --role lead
python3 scripts/claude-builder.py --role worker --worktree-name task-name
python3 scripts/claude-builder.py --role reviewer
```

Each launcher uses the existing private provider files and project configuration. `/doctor` remains available in the interactive CLI; cross-session usage is reported separately. There is no unlimited background inference swarm or automatic main-branch merge.
