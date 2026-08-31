#!/bin/bash
# Load the production schema DDL into the dev 'data' schema and stamp alembic.
#
# Why a dump and not `alembic upgrade head`: the migration chain's base
# revision (262385e9df48) has down_revision=None but its docstring says it
# revises 8c591955ca8a, which no longer exists in versions/. The chain only
# carries incremental changes on top of a schema that was originally created
# by Base.metadata.create_all(), so it cannot build from an empty database.
set -euo pipefail

cd /store/droptracker/disc
ENVF=/store/droptracker/disc/.env
DB_USER=$(grep -m1 '^DB_USER=' "$ENVF" | cut -d= -f2- | tr -d '"'"'"' ')
HEAD=web87a_developer_role

echo "==> loading schema into data"
sudo mariadb -e "DROP DATABASE IF EXISTS data; CREATE DATABASE data CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
zcat /tmp/data-schema.sql.gz | sudo mariadb data
COUNT=$(sudo mariadb -N -e "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='data';")
echo "    tables created: $COUNT"

echo "==> re-granting (DROP DATABASE dropped the grants)"
sudo mariadb -e "GRANT ALL PRIVILEGES ON data.* TO '${DB_USER}'@'127.0.0.1'; GRANT ALL PRIVILEGES ON data.* TO '${DB_USER}'@'::1'; FLUSH PRIVILEGES;"

echo "==> stamping alembic at prod head ${HEAD}"
venv/bin/alembic stamp "$HEAD" 2>&1 | tail -3
echo "    alembic_version: $(sudo mariadb -N -e 'SELECT version_num FROM data.alembic_version;')"

echo "==> alembic current"
venv/bin/alembic current 2>&1 | tail -2

echo "==> app connectivity through the ORM"
venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '/store/droptracker/disc')
from db.models.base import engine
from sqlalchemy import text
with engine.connect() as c:
    n = c.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='data'")).scalar()
    print("    ORM engine sees %d tables in data" % n)
    print("    drops columns:", c.execute(text("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='data' AND TABLE_NAME='drops'")).scalar())
PY
