#!/usr/bin/env python3
"""Build the DropTracker dev .env files from values already present on this box.

Source of truth is the preserved .env from this machine's own prior checkout,
so no production secret has to be transferred here in order to create them.

Run on the dev box:  python3 build_dev_env.py
"""
import os
import secrets
import subprocess
import datetime

SRC = "/store/droptracker-devsetup/preserved/old-dev.env"
DISC = "/store/droptracker/disc/.env"
WEB = "/store/droptracker/web/.env"

NOW = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

raw = subprocess.run(["sudo", "cat", SRC], capture_output=True, text=True).stdout
env = {}
for line in raw.splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip()

# Every bot needs its own dev Discord application. Blank rather than inherit:
# booting on a production token connects AS the production bot in real guilds.
BLANK_BOTS = [
    "BOT_TOKEN", "WEBHOOK_TOKEN", "HALL_OF_FAME_BOT_TOKEN", "HEARTBEAT_BOT_TOKEN",
    "TEST_BOT_TOKEN", "EVENT_BOT_TOKEN", "EV_BOT_TOKEN", "LOGGER_TOKEN",
    "HEARTBEAT_TOKEN", "DEV_TOKEN", "DEV_WEBHOOK_TOKEN",
]
for k in BLANK_BOTS:
    env[k] = ""

# Third-party write access this box must never hold.
DROP = [
    "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_RECORD_NAMES",
    "GITHUB_TOKEN", "B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME",
    "B2_ENDPOINT_URL", "B2_CDN_BASE_URL", "OPENAI_API_KEY", "PATREON_ACCESS_TOKEN",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "SENTRY_DSN", "SERVICE_ACCOUNT_FILE",
]
for k in DROP:
    env.pop(k, None)

env.update({
    "STATE": '"dev"',
    "STATUS": '"dev"',
    "DEBUG": "True",
    "DEBUG_LEVEL": "true",
    "WEB_SITE_URL": "https://dev.droptracker.io/",
    "NEXTAUTH_URL": "https://dev.droptracker.io/",
    "SITE_URL": "https://dev.droptracker.io/",
    "DISCORD_REDIRECT_URI": "https://dev.droptracker.io/api/auth/callback",
    "BOARD_IMAGE_BASE_URL": "http://127.0.0.1:31380",
    "WEBHOOK_TEMP_DIR": "/store/droptracker/disc/data/webhook_uploads",
    "WEBHOOK_QUEUE_MODE": "true",
    "WEBHOOK_CONSUMER_WORKERS": "2",
    "WEB_API_HOST": "127.0.0.1",
    "WEB_API_PORT": "31325",
    "DATA_DB_POOL_SIZE": "5",
    "DATA_DB_MAX_OVERFLOW": "15",
    "PRIMARY_GUILD_ID": "1172737525069135962",
    # Safety rails that must stay off on a dev box.
    "RECAP_DELIVERY_ENABLED": "false",
    "RECAP_DELIVERY_TEST_DISCORD_ID": "",
    "PROCESS_NITRO_BOOSTS": "False",
    "SHOULD_PROCESS_REACTIONS": "False",
    "DISCORD_MESSAGE_FOOTER": '"DEV instance | https://dev.droptracker.io/"',
})

# Generated keys that only need to be self-consistent within this box.
for k in ("BOARD_IMAGE_TOKEN", "MANUAL_SUBMIT_KEY", "HOOK_CREATION_KEY", "BACKEND_ACP_TOKEN"):
    if not env.get(k):
        env[k] = secrets.token_hex(32)

header = (
    "# DropTracker DEVELOPMENT environment\n"
    "# Built %s by the dev-box provisioning run.\n"
    "#\n"
    "# Discord bot tokens are intentionally EMPTY. Each bot needs its own dev\n"
    "# application before the matching unit is started -- running on production\n"
    "# tokens would connect as the production bot in real guilds.\n"
    "#\n"
    "# Cloudflare / GitHub / B2 / Stripe / Patreon / OpenAI / Sentry keys are\n"
    "# deliberately ABSENT so this box cannot write to those services.\n\n" % NOW
)

with open(DISC, "w") as f:
    f.write(header)
    for k in sorted(env):
        f.write("%s=%s\n" % (k, env[k]))
os.chmod(DISC, 0o600)

# web/.env -- JWT_TOKEN_KEY and BOARD_IMAGE_TOKEN must match disc on THIS box.
web = {
    "DISCORD_BOT_CLIENT_ID": env.get("DISCORD_BOT_CLIENT_ID", ""),
    "DISCORD_BOT_CLIENT_SECRET": env.get("DISCORD_BOT_CLIENT_SECRET", ""),
    "DISCORD_REDIRECT_URI": "https://dev.droptracker.io/api/auth/callback",
    "JWT_TOKEN_KEY": env.get("JWT_TOKEN_KEY", ""),
    "BOARD_IMAGE_TOKEN": env["BOARD_IMAGE_TOKEN"],
    "NEXT_PUBLIC_API_BASE": "/api",
    "WEB_API_INTERNAL_URL": "http://127.0.0.1:31325",
    "NEXTAUTH_URL": "https://dev.droptracker.io",
    "WEB_SITE_URL": "https://dev.droptracker.io/",
    "SESSION_COOKIE_SECURE": "false",
    "USE_MOCK_API": "false",
    "SESSION_COOKIE_SECRET": secrets.token_hex(32),
    "NEXT_SERVER_ACTIONS_ENCRYPTION_KEY": secrets.token_hex(32),
}
with open(WEB, "w") as f:
    f.write("# DropTracker DEVELOPMENT frontend env. Built %s\n" % NOW)
    f.write("# JWT_TOKEN_KEY and BOARD_IMAGE_TOKEN must match disc/.env on THIS box.\n\n")
    for k in sorted(web):
        f.write("%s=%s\n" % (k, web[k]))
os.chmod(WEB, 0o600)

print("disc/.env written: %d keys" % len(env))
print("web/.env written:  %d keys" % len(web))
print("blanked bot tokens: %s" % ", ".join(BLANK_BOTS))
print("removed third-party: %s" % ", ".join(DROP))
print("DB_USER=%s  (password carried over from this box's own prior .env)" % env.get("DB_USER"))
