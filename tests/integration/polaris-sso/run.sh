#!/usr/bin/env bash
set -euo pipefail
: "${MARIADB_IMAGE:=mariadb:10.11}"
name="phpretro-g03-sso-$RANDOM-$$"
cleanup(){ docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run --rm -d --name "$name" -e MARIADB_ROOT_PASSWORD=root "$MARIADB_IMAGE" --skip-name-resolve >/dev/null
for i in $(seq 1 60); do docker exec "$name" mariadb-admin --protocol=tcp -uroot -proot ping --silent >/dev/null 2>&1 && break; sleep 1; done
docker exec "$name" mariadb-admin --protocol=tcp -uroot -proot ping --silent >/dev/null
sql='CREATE DATABASE sso; USE sso; CREATE TABLE users (id INT PRIMARY KEY, auth_ticket VARCHAR(256) COLLATE utf8mb4_general_ci, auth_ticket_expires_at DATETIME NULL); INSERT INTO users VALUES (1,"AbC-123",CURRENT_TIMESTAMP + INTERVAL 1 MINUTE),(2,"stale",CURRENT_TIMESTAMP - INTERVAL 1 SECOND),(3,"equal",CURRENT_TIMESTAMP);'
docker exec "$name" mariadb --protocol=tcp -uroot -proot -e "$sql"
test "$(docker exec "$name" mariadb --protocol=tcp -N -uroot -proot -e "USE sso; UPDATE users SET auth_ticket='',auth_ticket_expires_at=NULL WHERE id=1 AND CAST(auth_ticket AS BINARY)=_binary'AbC-123' AND auth_ticket_expires_at > CURRENT_TIMESTAMP; SELECT ROW_COUNT();")" = 1
test "$(docker exec "$name" mariadb --protocol=tcp -N -uroot -proot -e "USE sso; UPDATE users SET auth_ticket='',auth_ticket_expires_at=NULL WHERE id=1 AND CAST(auth_ticket AS BINARY)=_binary'AbC-123' AND auth_ticket_expires_at > CURRENT_TIMESTAMP; SELECT ROW_COUNT();")" = 0
test "$(docker exec "$name" mariadb --protocol=tcp -N -uroot -proot -e "USE sso; SELECT COUNT(*) FROM users WHERE id IN (2,3) AND auth_ticket <> '';")" = 2
docker exec "$name" mariadb --protocol=tcp -uroot -proot -e "USE sso; UPDATE users SET auth_ticket='AbC-123',auth_ticket_expires_at=CURRENT_TIMESTAMP+INTERVAL 1 MINUTE WHERE id=1; UPDATE users SET auth_ticket='',auth_ticket_expires_at=NULL WHERE id=1 AND CAST(auth_ticket AS BINARY)=_binary'abc-123' AND auth_ticket_expires_at > CURRENT_TIMESTAMP; SELECT ROW_COUNT();" | grep -qx '0'
# At the expiry boundary and when another issuance has replaced the ticket, no stale consume.
docker exec "$name" mariadb --protocol=tcp -uroot -proot -e "USE sso; UPDATE users SET auth_ticket='fresh',auth_ticket_expires_at=CURRENT_TIMESTAMP+INTERVAL 1 MINUTE WHERE id=1; UPDATE users SET auth_ticket='',auth_ticket_expires_at=NULL WHERE id=1 AND CAST(auth_ticket AS BINARY)=_binary'AbC-123' AND auth_ticket_expires_at > CURRENT_TIMESTAMP; SELECT ROW_COUNT();" | grep -qx '0'
printf '%s\n' 'MariaDB strict expiry, binary exactness, replay and one-use checks passed'
