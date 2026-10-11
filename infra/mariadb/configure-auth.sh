#!/bin/sh
set -eu
case "$CMS_POLARIS_DB_PASSWORD" in ''|*[!a-f0-9]*) echo 'Invalid bridge credential format' >&2; exit 1;; esac
export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"
mariadb --host=mariadb --user=root <<SQL
CREATE USER IF NOT EXISTS 'cms_polaris_bridge'@'%' IDENTIFIED BY '$CMS_POLARIS_DB_PASSWORD';
GRANT UPDATE ON phpretro_cms.cms_settings TO 'cms_app'@'%';
GRANT INSERT ON phpretro_cms.cms_audit_events TO 'cms_app'@'%';
GRANT UPDATE (last_totp_step) ON phpretro_cms.cms_staff_roles TO 'cms_app'@'%';
SQL
tables=$(mariadb --host=mariadb --user=root --batch --skip-column-names -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='polaris' AND table_name IN ('users','bans')")
if [ "$tables" != 2 ]; then
  echo 'Polaris schema is not installed; identity login remains unavailable.'
  exit 0
fi
mariadb --host=mariadb --user=root <<SQL
GRANT SELECT (id,username,password,account_created,access_token_version,machine_id) ON polaris.users TO 'cms_polaris_bridge'@'%';
GRANT SELECT (user_id,ip,machine_id,type,ban_expire) ON polaris.bans TO 'cms_polaris_bridge'@'%';
SQL
echo 'Scoped identity bridge and CMS auth grants configured.'
