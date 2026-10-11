CREATE TABLE IF NOT EXISTS cms_staff_roles (
  user_id INT NOT NULL PRIMARY KEY,
  role ENUM('admin') NOT NULL,
  role_revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
  mfa_revision CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  mfa_secret VARBINARY(128) NOT NULL,
  last_totp_step BIGINT NOT NULL DEFAULT -1,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT cms_staff_positive_id CHECK (user_id > 0)
);
-- next-statement
CREATE TABLE IF NOT EXISTS cms_audit_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  actor_user_id INT NOT NULL,
  action VARCHAR(64) NOT NULL,
  before_json JSON NOT NULL,
  after_json JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX cms_audit_actor (actor_user_id, created_at)
);
