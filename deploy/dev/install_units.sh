#!/bin/bash
# Install DropTracker systemd units on the DEV box.
#
# Divergences from production, all deliberate:
#  * Everything runs as `debian`. Prod splits across `user` and `debian`, which
#    is why prod's asset tree is 0777. One user on dev removes that problem.
#  * Single Next.js colour (blue). Blue-green is a production uptime mechanism.
#  * Every Discord bot unit is installed but NOT enabled - their tokens are
#    blank pending new dev applications.
#  * Units with no dev gate in code (hof, heartbeat) are MASKED, not just
#    disabled, so a stray `systemctl start` cannot connect them to Discord.
#  * All timers masked: recaps can DM thousands of users, wom-sync pushes to
#    a GitHub repo, prune-images deletes test screenshots.
set -euo pipefail

SRC=/store/droptracker/disc/deploy/systemd
DST=/etc/systemd/system

# unit -> disposition
ENABLE=(droptracker-api droptracker-webapi droptracker-webhook-consumer
        droptracker-events droptracker-lootboards droptracker-node-blue)
INSTALL_ONLY=(droptracker-core droptracker-webhooks droptracker-api-dev
              droptracker-node-green droptracker-player-updates
              droptracker-video-worker)
MASK=(droptracker-hof droptracker-heartbeat)
MASK_TIMERS=(droptracker-recaps droptracker-wom-sync droptracker-prune-images
             droptracker-health-watch droptracker-npc-ehb-rates droptracker-db-backup)

echo "==> installing unit files"
for u in "${ENABLE[@]}" "${INSTALL_ONLY[@]}" "${MASK[@]}"; do
  [ -f "$SRC/$u.service" ] && sudo cp "$SRC/$u.service" "$DST/$u.service" && echo "    $u.service"
done

echo "==> writing dev drop-ins"
for u in "${ENABLE[@]}" "${INSTALL_ONLY[@]}"; do
  [ -f "$DST/$u.service" ] || continue
  sudo install -d -m 755 "$DST/$u.service.d"
  sudo tee "$DST/$u.service.d/10-dev.conf" >/dev/null <<'DROPIN'
# DropTracker DEV overrides. Canonical copy: disc/deploy/dev/systemd/
[Service]
# Prod splits services across `user` and `debian`; dev runs everything as one
# account so the shared asset tree needs no 0777 workaround.
User=debian
Group=debian
UMask=0002
# chromium (board screenshots) needs a writable HOME.
ProtectHome=false
Environment=HOME=/home/debian
# Keep one runaway process from taking down a shared box.
MemoryMax=6G
MemoryHigh=4G
# A dev box gets restarted a lot; don't let a crash-loop hide the real error.
StartLimitIntervalSec=300
StartLimitBurst=5
DROPIN
done

echo "==> masking units that have no dev gate in code"
for u in "${MASK[@]}"; do
  sudo systemctl mask "$u.service" 2>/dev/null || true
  echo "    masked $u.service"
done

echo "==> masking all timers"
for t in "${MASK_TIMERS[@]}"; do
  sudo systemctl mask "$t.timer" 2>/dev/null || true
  echo "    masked $t.timer"
done

sudo systemctl daemon-reload

echo "==> enabling the dev service set"
for u in "${ENABLE[@]}"; do
  sudo systemctl enable "$u.service" >/dev/null 2>&1 && echo "    enabled $u"
done

echo "==> leaving these installed but stopped (need dev Discord tokens / not wanted on dev)"
for u in "${INSTALL_ONLY[@]}"; do
  sudo systemctl disable "$u.service" >/dev/null 2>&1 || true
  echo "    installed-disabled $u"
done

echo "==> done. Nothing started yet."
