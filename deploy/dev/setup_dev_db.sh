#!/bin/bash
# Set up MariaDB + Redis auth + alembic.ini on the DropTracker dev box.
# Reads credentials from the .env already written on this machine.
set -euo pipefail

ENVF=/store/droptracker/disc/.env
DB_USER=$(grep -m1 '^DB_USER=' "$ENVF" | cut -d= -f2- | tr -d '"'"'"' ')
DB_PASS=$(grep -m1 '^DB_PASS=' "$ENVF" | cut -d= -f2- | tr -d '"'"'"' ')

if [ -z "$DB_USER" ] || [ -z "$DB_PASS" ]; then
  echo "FATAL: DB_USER/DB_PASS not found in $ENVF" >&2; exit 1
fi
echo "==> DB_USER=$DB_USER (password read from .env, not echoed)"

# --- schemas -------------------------------------------------------------
# db/models/base.py hardcodes @localhost:3306/data and /xenforo, so the
# schema names are not negotiable.
sudo mariadb <<SQL
CREATE DATABASE IF NOT EXISTS data    CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE DATABASE IF NOT EXISTS xenforo CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
SQL
echo "==> schemas data + xenforo created"

# --- app user ------------------------------------------------------------
# Grant over TCP hosts only. root@localhost keeps its unix_socket auth so
# 'sudo mariadb' continues to work for administration.
sudo mariadb <<SQL
CREATE USER IF NOT EXISTS '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASS}';
ALTER USER '${DB_USER}'@'127.0.0.1' IDENTIFIED BY '${DB_PASS}';
CREATE USER IF NOT EXISTS '${DB_USER}'@'::1' IDENTIFIED BY '${DB_PASS}';
ALTER USER '${DB_USER}'@'::1' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON data.*    TO '${DB_USER}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON xenforo.* TO '${DB_USER}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON data.*    TO '${DB_USER}'@'::1';
GRANT ALL PRIVILEGES ON xenforo.* TO '${DB_USER}'@'::1';
FLUSH PRIVILEGES;
SQL
echo "==> user ${DB_USER}@127.0.0.1 granted on data + xenforo"

# --- redis auth ----------------------------------------------------------
# utils/redis.py does REDIS_PW = os.getenv('DB_PASS'), so Redis must require
# exactly that password or every connection fails on AUTH.
sudo sed -i '/^requirepass /d' /etc/redis/redis.conf
echo "requirepass ${DB_PASS}" | sudo tee -a /etc/redis/redis.conf >/dev/null
sudo systemctl restart redis-server
sleep 2
if redis-cli -a "${DB_PASS}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
  echo "==> redis requirepass set and verified"
else
  echo "FATAL: redis auth failed" >&2; exit 1
fi

# --- alembic.ini ---------------------------------------------------------
cd /store/droptracker/disc
if [ -f alembic.ini.template ]; then cp -n alembic.ini.template alembic.ini 2>/dev/null || true; fi
if [ ! -f alembic.ini ]; then echo "FATAL: no alembic.ini or template" >&2; exit 1; fi
python3 - "$DB_USER" "$DB_PASS" <<'PY'
import re, sys, urllib.parse
user, pw = sys.argv[1], sys.argv[2]
url = "mysql+pymysql://%s:%s@localhost:3306/data" % (user, urllib.parse.quote_plus(pw))
p = "/store/droptracker/disc/alembic.ini"
s = open(p).read()
if re.search(r'(?m)^sqlalchemy\.url\s*=', s):
    s = re.sub(r'(?m)^sqlalchemy\.url\s*=.*$', "sqlalchemy.url = " + url, s)
else:
    s = s.replace("[alembic]", "[alembic]\nsqlalchemy.url = " + url, 1)
open(p, "w").write(s)
PY
chmod 600 alembic.ini
echo "==> alembic.ini written (mode 600)"

# --- connectivity smoke test --------------------------------------------
venv/bin/python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv('/store/droptracker/disc/.env')
import pymysql, redis
c = pymysql.connect(host='localhost', port=3306,
                    user=os.getenv('DB_USER'), password=os.getenv('DB_PASS'),
                    database='data')
cur = c.cursor(); cur.execute('SELECT VERSION()')
print("==> pymysql -> MariaDB", cur.fetchone()[0])
c.close()
r = redis.Redis(host='127.0.0.1', port=6379, db=0, password=os.getenv('DB_PASS'))
print("==> redis ping:", r.ping())
PY
echo "==> DB layer ready"
