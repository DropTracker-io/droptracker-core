#!/bin/bash
# Launch the Web API v1 (:31325) in the foreground. Normally this runs under
# systemd (droptracker-webapi.service) — prefer `systemctl restart
# droptracker-webapi`. Only run this script directly for ad-hoc debugging,
# after stopping the unit so the port is free.
cd /store/droptracker/disc || exit 1
exec ./venv/bin/hypercorn --workers 2 --worker-class asyncio \
  --keep-alive 5 --graceful-timeout 10 \
  --bind 127.0.0.1:31325 "web_api:create_app()"
