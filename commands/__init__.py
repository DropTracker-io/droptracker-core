"""
Commands Package

This package contains all Discord slash commands for the DropTracker bot,
organized by permission level and functionality for better maintainability.

Modules:
    user: User-level commands (help, accounts, claim-rsn, dm-settings, etc.)
    admin: Administrator commands (group management, webhooks, etc.)
    group_admin: Group admin commands (manual point adjustments, audit log)
    submissions: Manual submission commands (/submit drop|clog|pb|ca|pet)
    utils: Utility functions and helpers for commands

Classes:
    UserCommands: Extension containing user-level commands
    ClanCommands: Extension containing clan/admin commands
    GroupAdminCommands: Extension containing group admin point management commands
    SubmissionCommands: Extension containing the /submit manual-submission commands

Author: joelhalen
"""

from .user import UserCommands
from .admin import ClanCommands
from .group_admin import GroupAdminCommands
from .submissions import SubmissionCommands
from .utils import try_create_user, is_admin, is_user_authorized, get_external_latency

__all__ = [
    'UserCommands',
    'ClanCommands',
    'GroupAdminCommands',
    'SubmissionCommands',
    'try_create_user',
    'is_admin',
    'is_user_authorized',
    'get_external_latency'
]
