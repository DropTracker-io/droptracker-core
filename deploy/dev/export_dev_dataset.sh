#!/bin/bash
# Export a light-scrub dev dataset from PRODUCTION.
#
# Scrub posture (per owner decision 2026-08-06): keep real names/IDs, but the
# tables carrying live Discord write-access never leave this box at all --
# excluding them from the dump is stronger than nulling them after transfer.
#
# Excluded entirely:
#   webhooks, backup_webhooks, webhook_pending_deletion, new_webhooks
#       -> 1,619 live clan webhook URLs = write access into third-party servers
#   notification_queue
#       -> rendered Discord payloads embedding channel/webhook targets
#   player_item_hourly_totals, player_npc_hourly_totals
#       -> 10.7 GB of pure derived aggregates; rebuilt on dev from drops
#   drops
#       -> dumped separately as a 7-day slice
#
# Post-load on dev: users.auth_token and export_api_key are blanked.
set -euo pipefail

OUT=/store/droptracker/devdata
DAYS=7
mkdir -p "$OUT"

# Credentials via a 0600 defaults-file so the password never appears in `ps`.
CNF=$(mktemp); chmod 600 "$CNF"
trap 'rm -f "$CNF"' EXIT
python3 - "$CNF" <<'PY'
import re, sys, configparser
c = configparser.ConfigParser(); c.read('/store/droptracker/disc/alembic.ini')
m = re.match(r'mysql\+pymysql://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)', c['alembic']['sqlalchemy.url'])
open(sys.argv[1], 'w').write(
    "[client]\nuser=%s\npassword=\"%s\"\nhost=%s\nport=%s\n" % (m.group(1), m.group(2), m.group(3), m.group(4)))
PY

EXCLUDE=(
  drops
  player_item_hourly_totals player_npc_hourly_totals
  webhooks backup_webhooks webhook_pending_deletion new_webhooks
  notification_queue
)
IGN=""
for t in "${EXCLUDE[@]}"; do IGN="$IGN --ignore-table=data.$t"; done

echo "==> [1/2] dumping reference + small tables (data-only)"
# shellcheck disable=SC2086
mysqldump --defaults-file="$CNF" \
  --single-transaction --quick --skip-lock-tables --no-create-info \
  --no-tablespaces --skip-add-locks --disable-keys \
  $IGN data 2>/dev/null | gzip -1 > "$OUT/dev-tables.sql.gz"
echo "    $(du -h "$OUT/dev-tables.sql.gz" | cut -f1)"

echo "==> [2/2] dumping ${DAYS}-day drops slice"
mysqldump --defaults-file="$CNF" \
  --single-transaction --quick --skip-lock-tables --no-create-info \
  --no-tablespaces --skip-add-locks --disable-keys \
  --where="date_added >= DATE_SUB(UTC_DATE(), INTERVAL ${DAYS} DAY)" \
  data drops 2>/dev/null | gzip -1 > "$OUT/dev-drops.sql.gz"
echo "    $(du -h "$OUT/dev-drops.sql.gz" | cut -f1)"

echo "==> leak check: no Discord webhook URLs may appear in either artifact"
HITS=$(zcat "$OUT"/dev-*.sql.gz | grep -cE 'discord(app)?\.com/api/webhooks' || true)
echo "    webhook URL occurrences: $HITS"
if [ "$HITS" != "0" ]; then echo "FATAL: webhook URLs present in artifact" >&2; exit 1; fi

echo "==> structural guard: artifact must not switch databases"
BAD=$(zcat "$OUT"/dev-*.sql.gz | grep -cE '^(CREATE DATABASE|USE )' || true)
echo "    CREATE DATABASE / USE statements: $BAD"
if [ "$BAD" != "0" ]; then echo "FATAL: artifact can escape its target schema" >&2; exit 1; fi

sha256sum "$OUT"/dev-tables.sql.gz "$OUT"/dev-drops.sql.gz > "$OUT/SHA256SUMS"
echo "==> done"
ls -la "$OUT"
