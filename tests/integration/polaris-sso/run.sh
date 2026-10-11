#!/usr/bin/env bash
set -euo pipefail
patch=integrations/polaris/patches/0001-strict-sso-consumer.patch
test -s "$patch"
grep -q 'CAST(auth_ticket AS BINARY)' "$patch"
grep -q 'auth_ticket_expires_at > CURRENT_TIMESTAMP' "$patch"
grep -q 'setAutoCommit(false)' "$patch"
grep -q 'debug_sso' "$patch"
! grep -q 'restoreSsoTicket' "$patch"
printf '%s\n' 'strict SSO patch contract checks passed (MariaDB fixture not available in this environment)'
