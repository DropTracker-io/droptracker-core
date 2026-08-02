# Host-level drop-ins (NOT DropTracker units)

These constrain things outside the application — the database and interactive
sessions — so a runaway on this box cannot take production down. They live here
for reproducibility; the app's own units are one directory up.

Written after the 2026-08-02 incident: a hung test process grew to 31 GiB on a
62 GiB box, the kernel OOM-killed MariaDB (always the largest process at ~19 GiB),
and that took the intake API with it for 2h31m.

## 1. Interactive/session memory cap — APPLIED

Bounds every SSH/agent session collectively. Production services live in
`system.slice` and are untouched; only logged-in sessions and hand-run commands
are capped. Normal usage here is ~6 GiB.

    sudo systemctl set-property user.slice MemoryHigh=12G MemoryMax=20G

`MemoryHigh` throttles via reclaim first, `MemoryMax` is the hard kill. Already
applied and persisted by systemd under /etc/systemd/system.control/.
Verified: a deliberate allocator is killed at the limit.

## 2. MariaDB OOM protection — NEEDS ROOT, NOT YET PERSISTENT

`OOMScoreAdjust` cannot be set at runtime (systemd applies it at fork), and
writing under /etc/systemd/system/ was not permitted, so this is currently set
only on the RUNNING process and will be lost on the next MariaDB restart:

    echo -800 | sudo tee /proc/$(pgrep -x mariadbd)/oom_score_adj

To make it permanent, install mariadb-oom-protect.conf from this directory:

    sudo mkdir -p /etc/systemd/system/mariadb.service.d
    sudo cp deploy/systemd/host/mariadb-oom-protect.conf \
            /etc/systemd/system/mariadb.service.d/oom-protect.conf
    sudo systemctl daemon-reload
    # takes effect on the next MariaDB restart; no restart needed now, the
    # running process is already protected by the echo above.

This does not make the database immune — it stops it being the *default* victim,
which it otherwise always is by virtue of being the biggest process.
