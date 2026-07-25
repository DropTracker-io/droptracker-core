"""Discord-driven Claude Code agent sessions (INCOMPLETE — see policy.py).

Only :mod:`services.agent.policy` exists so far: workspaces, the capability
allowlist, and the turn-control marker protocol. The runner that actually
spawns and drives a ``claude`` process, and the ``/agent`` command surface that
would reach it from Discord, are not implemented — nothing imports this package
yet, so it is inert.
"""
