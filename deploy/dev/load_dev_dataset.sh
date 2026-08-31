#!/bin/bash
# Load the light-scrub dev dataset into this box's 'data' schema and apply
# the post-load scrub (auth tokens, export API keys, invite URLs).
set -euo pipefail

cd /store/droptracker/disc
ENVF=/store/droptracker/disc/.env
DB_USER=$(grep -m1 '^DB_USER=' "$ENVF" | cut -d= -f2- | tr -d '"'"'"' ')

echo "==> verifying checksums"
cd /tmp && sha256sum -c SHA256SUMS
cd /store/droptracker/disc

echo "==> loading reference + small tables"
{ echo "SET FOREIGN_KEY_CHECKS=0; SET UNIQUE_CHECKS=0; SET AUTOCOMMIT=0;";
  zcat /tmp/dev-tables.sql.gz;
  echo "COMMIT; SET FOREIGN_KEY_CHECKS=1; SET UNIQUE_CHECKS=1;"; } | sudo mariadb data
echo "    done"

echo "==> loading drops slice (5.7M rows, this takes a few minutes)"
{ echo "SET FOREIGN_KEY_CHECKS=0; SET UNIQUE_CHECKS=0; SET AUTOCOMMIT=0;";
  zcat /tmp/dev-drops.sql.gz;
  echo "COMMIT; SET FOREIGN_KEY_CHECKS=1; SET UNIQUE_CHECKS=1;"; } | sudo mariadb data
echo "    done"

echo "==> post-load scrub"
sudo mariadb data <<'SQL'
-- Auth tokens: 600 live values on prod. Blank them all.
-- '' not NULL: the column is NOT NULL, and with set -e a failure here
-- silently skips every scrub below it, leaving live export keys on the box.
UPDATE users SET auth_token = '' WHERE auth_token IS NOT NULL AND auth_token <> '';
-- Group export API keys: 261 live 40-char keys.
UPDATE group_configurations SET config_value = '' WHERE config_key = 'export_api_key';
-- Invite URLs: one row held a pasted webhook URL (already redacted in transit).
UPDATE groups SET invite_url = NULL WHERE invite_url IS NOT NULL;
-- These four were never dumped; assert they are empty rather than assume it.
SQL
echo "    scrub applied"

echo "==> verification"
sudo mariadb -N data <<'SQL'
SELECT CONCAT('    players:              ', COUNT(*)) FROM players;
SELECT CONCAT('    users:                ', COUNT(*)) FROM users;
SELECT CONCAT('    groups:               ', COUNT(*)) FROM groups;
SELECT CONCAT('    items:                ', COUNT(*)) FROM items;
SELECT CONCAT('    npc_list:             ', COUNT(*)) FROM npc_list;
SELECT CONCAT('    drops:                ', COUNT(*)) FROM drops;
SELECT CONCAT('    collection:           ', COUNT(*)) FROM collection;
SELECT CONCAT('    personal_best:        ', COUNT(*)) FROM personal_best;
SELECT CONCAT('    group_configurations: ', COUNT(*)) FROM group_configurations;
SELECT CONCAT('  -- must all be ZERO --') ;
SELECT CONCAT('    webhooks:             ', COUNT(*)) FROM webhooks;
SELECT CONCAT('    backup_webhooks:      ', COUNT(*)) FROM backup_webhooks;
SELECT CONCAT('    new_webhooks:         ', COUNT(*)) FROM new_webhooks;
SELECT CONCAT('    notification_queue:   ', COUNT(*)) FROM notification_queue;
SELECT CONCAT('    users w/ auth_token:  ', COUNT(*)) FROM users WHERE auth_token IS NOT NULL AND auth_token <> '';
SELECT CONCAT('    live export_api_key:  ', COUNT(*)) FROM group_configurations WHERE config_key='export_api_key' AND config_value <> '';
SQL

echo "==> drops date range on dev"
sudo mariadb -N data -e "SELECT CONCAT('    ', MIN(date_added), '  ..  ', MAX(date_added)) FROM drops;"
