"""What the RuneLite plugin is allowed to receive from us.

The Plugin Hub maintainers require that every host the plugin connects to is
hardcoded in the plugin or typed by the user:

    "you cannot dynamically/programmatically retrieve URLs from other API
    responses and call into them, that is an ssrf risk and it prevents us from
    being able to exhaustively review the domains that your plugin connects to
    without user input. this includes just fetching things like images as well
    as doing any other API requests"

So the API never hands the plugin an address. Images travel as paths relative to
``/img/``; Discord webhooks travel as an ``<id>/<token>`` credential pair. The
plugin owns the host in both cases (see ``io.droptracker.api.DropTrackerUrls``
in the plugin repo).

This module is deliberately pure — stdlib only, no DB, no network — so it can be
imported from the intake API, the GitHub Pages publisher and the tests alike.
"""

# Prefixes that are already served from our own image host.
IMG_BASE_PREFIXES = (
    "https://www.droptracker.io/img/",
    "https://droptracker.io/img/",
)

_INVITE_MARKERS = (
    "discord.gg/",
    "discordapp.com/invite/",
    "discord.com/invite/",
)


def img_relative(url):
    """Relative ``/img/`` path for an asset URL already on our host, else None.

    Used for free-form columns (a group's icon, a team's board piece) that may
    point anywhere: anything off-host resolves to "no image" rather than to a
    fetch the plugin is not allowed to make.
    """
    if not url:
        return None
    value = str(url).strip()
    for prefix in IMG_BASE_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix):] or None
    return None


def webhook_credentials(webhook_url):
    """``"<id>/<token>"`` for a Discord webhook URL, or None if it is not one.

    The published webhook list carries credentials rather than whole URLs, so the
    plugin rebuilds ``https://discord.com/api/webhooks/<id>/<token>`` from its own
    constant. The plugin's parser still accepts the legacy full-URL form, so
    switching the publisher over needs no coordinated plugin release.
    """
    if not webhook_url:
        return None
    value = str(webhook_url).strip()
    marker = "/api/webhooks/"
    index = value.find(marker)
    if index >= 0:
        value = value[index + len(marker):]
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = value.split("/")
    if len(parts) != 2:
        return None
    webhook_id, token = parts
    if not webhook_id.isdigit() or not token:
        return None
    if not all(character.isalnum() or character in "._-" for character in token):
        return None
    return f"{webhook_id}/{token}"


def discord_invite_code(invite_url):
    """The bare invite code from a stored Discord invite URL, or None.

    The plugin builds ``https://discord.gg/<code>`` itself rather than opening a
    URL we chose for it — same rule as the images above.
    """
    if not invite_url:
        return None
    value = str(invite_url).strip().rstrip("/")
    if not value:
        return None
    for marker in _INVITE_MARKERS:
        index = value.rfind(marker)
        if index >= 0:
            value = value[index + len(marker):]
            break
    else:
        # A bare code is fine; anything else with structure is not an invite.
        if "/" in value or ":" in value:
            return None
    code = value.split("?", 1)[0].split("#", 1)[0]
    if not code or "/" in code:
        return None
    # Discord invite codes are alphanumeric with hyphens; anything else is not one.
    if not all(character.isalnum() or character == "-" for character in code):
        return None
    return code
