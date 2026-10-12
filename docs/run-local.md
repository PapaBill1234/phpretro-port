# Start the CMS development stack

Requirements: Docker Engine with Compose, Python 3, and a free local port 8789. The stack uses project-owned containers and a new MariaDB volume; the previous preservation/runtime projects are not used as live services.

```bash
python3 scripts/dev-setup.py
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8789/api/v1/ready
```

Open `http://127.0.0.1:8789`. The setup script creates a mode-0600 ignored `.env` containing fresh development database/cache passwords. It preserves an existing file. Provider credentials are separate and are never passed to the CMS containers.

When upgrading the previous foundation, run `python3 scripts/dev-setup.py --upgrade` before rebuilding. This adds missing bridge/MFA keys and local operator UID/GID without rotating existing database/cache credentials. It requires an owned regular mode-0600 `.env`. Keep the MFA encryption key backed up privately; losing it makes enrolled secrets unreadable.

MariaDB and Redis have no published host ports. The one-shot migrator uses `cms_migrator`; CMS uses limited settings/audit/MFA grants and a separate read-only Polaris identity bridge. The empty `polaris` database must receive the compatible real Polaris schema/data before existing hotel accounts can log in. This command does not install or start Polaris/Octane. Until identity tables are installed, health and public settings work, login fails closed, and readiness reports `identity:false` with HTTP503. After installing the schema, run `docker compose run --rm --no-deps configure-auth` and check readiness again. Do not use integration fixtures as a real hotel database.

Existing Polaris accounts can sign in and out. CMS staff require a separately enrolled TOTP credential; game rank does not grant CMS administration. Housekeeping persists site name, active theme, enabled languages and default language with revision conflicts and transactional audit. Two original theme shells and en/nl presentation are implemented; complete layout parity, registration/reset, game launch and widget replacements remain open.

Enroll an existing positive Polaris user ID, replacing `123` with that identity:

```bash
install -d -m 700 .builder/enrollment
docker compose --profile ops run --rm --no-deps operator --grant-admin 123 --enrollment-file /enrollment/123.json
```

The new mode-0600 file contains the TOTP secret and authenticator URI. Import it privately into the operator's authenticator. The command refuses to overwrite a file or an existing enrollment. No secret is printed and no default administrator exists. `LOCAL_UID` and `LOCAL_GID` match the user who generated `.env`, so the file remains readable by that local operator.

Revoke enrollment and invalidate that user's previous staff sessions on their next request:

```bash
docker compose --profile ops run --rm --no-deps operator --revoke-admin 123
```

For an isolated synthetic test, use the dedicated project/port procedure in [auth integration](auth-integration.md). Never point its fixture runner at a populated hotel.

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
