#!/bin/sh
set -eu

# Passwords are generated as hex; reject unexpected characters before SQL interpolation.
for credential in "$CMS_DB_PASSWORD" "$CMS_MIGRATION_DB_PASSWORD" "$POLARIS_DB_PASSWORD"; do
  case "$credential" in
    ''|*[!a-f0-9]*) echo 'Invalid local database credential format' >&2; exit 1 ;;
  esac
done

MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb --user=root <<SQL
CREATE DATABASE IF NOT EXISTS phpretro_cms CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS polaris CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'cms_app'@'%' IDENTIFIED BY '$CMS_DB_PASSWORD';
CREATE USER IF NOT EXISTS 'cms_migrator'@'%' IDENTIFIED BY '$CMS_MIGRATION_DB_PASSWORD';
CREATE USER IF NOT EXISTS 'polaris_app'@'%' IDENTIFIED BY '$POLARIS_DB_PASSWORD';
GRANT SELECT ON phpretro_cms.* TO 'cms_app'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX ON phpretro_cms.* TO 'cms_migrator'@'%';
GRANT ALL PRIVILEGES ON polaris.* TO 'polaris_app'@'%';
SQL
