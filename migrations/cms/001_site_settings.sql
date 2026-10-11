CREATE TABLE IF NOT EXISTS cms_settings (
  id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
  site_name VARCHAR(128) NOT NULL,
  active_theme VARCHAR(64) NOT NULL,
  default_locale VARCHAR(16) NOT NULL,
  enabled_locales JSON NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT cms_settings_singleton CHECK (id = 1)
);
-- seed-transaction
INSERT INTO cms_settings (id, site_name, active_theme, default_locale, enabled_locales, revision)
SELECT 1, 'PHPRetro Port', 'modern', 'en', '["en","nl"]', 1
WHERE NOT EXISTS (SELECT 1 FROM cms_settings WHERE id = 1);
