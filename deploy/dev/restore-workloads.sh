#!/bin/bash
# Restore the workloads that DropTracker dev-env setup stopped on 2026-08-06.
# NOTHING WAS DELETED - every project directory is untouched on disk.
# Run with: sudo bash /store/droptracker-devsetup/restore-workloads.sh
set -u

echo "== Palworld game server =="
sudo systemctl enable --now palworld.service
echo "   (was: enabled + running, ports udp/8211 udp/27015, tcp/25575 blocked by iptables)"

echo
echo "== Game project (/home/debian/game) =="
echo "   These were started by hand, not systemd. Restart from that directory:"
echo "     cd /home/debian/game        && node scripts/serve-apk.mjs &"
echo "     cd /home/debian/game/apps/client      && node ../../node_modules/.bin/vite &"
echo "     cd /home/debian/game/apps/game-server && node ../../node_modules/.bin/tsx watch --clear-screen=false src/index.ts &"

echo
echo "== zombies-rip (/home/debian/zombies-rip) =="
echo "   Ten instances, one per port. Restart with:"
echo "     cd /home/debian/zombies-rip"
echo "     for p in 8100 8111 8112 8113 8114 8121 8130 8140 8150 8151; do node server.js \$p & done"

echo
echo "== compliance (/store/compliance) =="
echo "     cd /store/compliance && node src/server.js &   # listened on :9050"

echo
echo "== PostgreSQL =="
echo "   Was NOT stopped - still running on 127.0.0.1:5432."
