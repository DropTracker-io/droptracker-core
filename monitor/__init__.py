"""Systemd integration helpers.

The only live module here is :mod:`monitor.sdnotifier`, which every
long-running process uses to talk to the systemd watchdog
(``Type=notify`` units — see ``deploy/systemd/``).

The old GNU-screen ServiceSpec registry and ``python -m monitor`` CLI that
used to live in this package were removed 2026-07-06: every process it
managed now runs as a systemd unit, and its exec commands pointed at entry
points that no longer exist. Use ``systemctl {start,stop,status}
'droptracker-*'`` instead.
"""
