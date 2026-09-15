"""Repair the Discord-webhook pool after the 2026-09-15 incident.

Dry-run by default; ``--apply`` writes. Idempotent: every step re-derives its
plan from live state, so a second run finds nothing to do.

  defuse   webhook_pending_deletion rows with no webhook_url. The heartbeat
           bot's old cleanup loop deleted each such row's whole channel 96h
           after it was queued (removed from bots/heartbeat.py); the rows are
           leftovers.
  restore  webhooks the GitHub Pages publisher deleted while they were alive:
           every webhook in a published snapshot of the pages repo
           (--restore-ref, default the last commit before the incident) that
           is no longer in the webhooks table, still appears in a pool guild's
           webhook list, and answers 200.
  purge    webhooks rows whose webhook no longer exists: absent from every
           guild webhook list the heartbeat bot can see AND answering 404
           Unknown Webhook (10015). A row needs both; anything inconclusive
           stays. Refuses when too many probes were inconclusive (Discord
           trouble) or when more than --max-purge-ratio of the table would go.

Usage (from the repo root):
    ./venv/bin/python -m scripts.repair_webhook_pool                  # dry-run
    ./venv/bin/python -m scripts.repair_webhook_pool --apply
    ./venv/bin/python -m scripts.repair_webhook_pool --steps restore --apply

Prints webhook ids only, never URLs (they carry the token).
"""
import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter

import aiohttp
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from utils.github import ALIVE, DEAD, INCONCLUSIVE, PoolRow, probe_webhook, run_is_degraded  # noqa: E402
from utils.plugin_urls import webhook_credentials  # noqa: E402

PAGES_REPO = "droptracker-io/droptracker-io.github.io"
# The last pages commit before the 14:44 UTC run deleted all 120 published webhooks.
DEFAULT_RESTORE_REF = "652a579c882335a5bf260256a0f6ad49af18d430"
WEBHOOK_FILE_RE = re.compile(r"^(?:core|\d{8}(?:-1)?)\.json$")
DISCORD_API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://www.droptracker.io, 1.0)"
# Pool guild -> webhooks.type, matching the categories in bots/heartbeat.py.
GUILD_TYPES = {
    "1172737525069135962": "core",
    "900855778095800380": "hooks",
    "597397938989432842": "hooks-2",
    "702992720909828168": "hooks-3",
}
STEPS = ("defuse", "restore", "purge")
PROBE_WORKERS = 3
PROBE_SPACING_SECONDS = 0.2


# --- planning (pure) -----------------------------------------------------------

def plan_defuse(pending_rows):
    """Pending-deletion rows without a URL. Rows are ``(id, webhook_id, webhook_url, channel_id)``."""
    return [row for row in pending_rows if not row[2]]


def plan_restore(snapshot, table_rows, listing, verdicts):
    """Snapshot webhooks to re-add: not in the table (by id or url), present in a
    guild listing, and answering alive. ``snapshot`` maps webhook id -> url,
    ``listing`` webhook id -> guild id, ``verdicts`` url -> verdict.
    Returns ``(to_restore [(webhook_id, url, type)], skipped Counter)``."""
    table_ids = {row.webhook_id for row in table_rows}
    table_urls = {row.url for row in table_rows}
    restore, skipped = [], Counter()
    for webhook_id, url in sorted(snapshot.items()):
        if webhook_id in table_ids or url in table_urls:
            skipped["already in table"] += 1
        elif webhook_id not in listing:
            skipped["not in any guild listing"] += 1
        elif verdicts.get(url) != ALIVE:
            skipped[f"probe {verdicts.get(url, 'missing')}"] += 1
        else:
            restore.append((webhook_id, url, GUILD_TYPES.get(listing[webhook_id], "core")))
    return restore, skipped


def plan_purge(table_rows, listing, verdicts):
    """Rows absent from every guild listing whose probe answered dead.
    Returns ``(to_purge [PoolRow], kept Counter)`` — kept counts the absent rows
    that stay, by verdict."""
    purge, kept = [], Counter()
    for row in table_rows:
        if row.webhook_id in listing:
            continue
        if verdicts.get(row.url) == DEAD:
            purge.append(row)
        else:
            kept[verdicts.get(row.url, "missing")] += 1
    return purge, kept


def purge_refusal(probed, inconclusive, purge_count, table_count, max_ratio):
    """Why the purge must not run, or None."""
    if run_is_degraded(probed, inconclusive):
        return f"{inconclusive} of {probed} probes were inconclusive (Discord or network trouble); try again later"
    if table_count and purge_count / table_count > max_ratio:
        return (f"it would delete {purge_count} of {table_count} rows, more than {max_ratio:.0%}; "
                "pass --max-purge-ratio if that is really intended")
    return None


# --- live state ------------------------------------------------------------------

def load_table():
    from db.models import Session, Webhook, WebhookPendingDeletion

    with Session() as s:
        rows, types = [], {}
        for r in s.query(Webhook.id, Webhook.webhook_id, Webhook.webhook_url, Webhook.type).all():
            rows.append(PoolRow(r.id, str(r.webhook_id) if r.webhook_id else None, r.webhook_url))
            types[r.id] = r.type
        pending = [
            (r.id, r.webhook_id, r.webhook_url, r.channel_id)
            for r in s.query(WebhookPendingDeletion.id, WebhookPendingDeletion.webhook_id,
                             WebhookPendingDeletion.webhook_url, WebhookPendingDeletion.channel_id).all()
        ]
    return rows, types, pending


def load_snapshot(ref):
    """webhook id -> url for every webhook in the pages repo's webhook files at ``ref``."""
    from github import Github

    from utils.encrypter import decrypt_webhook, get_encryption_key

    repo = Github(os.getenv("GITHUB_TOKEN")).get_repo(PAGES_REPO)
    key = get_encryption_key()
    snapshot = {}
    for item in repo.get_contents("content", ref=ref):
        if not WEBHOOK_FILE_RE.match(item.name):
            continue
        for entry in json.loads(repo.get_contents(item.path, ref=ref).decoded_content):
            try:
                url = decrypt_webhook(entry, encryption_key=key)
            except Exception:
                continue
            credentials = webhook_credentials(url)
            if credentials:
                snapshot[credentials.split("/", 1)[0]] = url
    return snapshot


async def _discord_get(http, path):
    for _ in range(5):
        async with http.get(DISCORD_API + path, timeout=aiohttp.ClientTimeout(total=20)) as response:
            if response.status == 429:
                body = await response.json(content_type=None)
                await asyncio.sleep(float(body.get("retry_after", 2)) + 0.5)
                continue
            if response.status != 200:
                raise RuntimeError(f"GET {path} answered HTTP {response.status}")
            return await response.json()
    raise RuntimeError(f"GET {path} kept answering 429")


async def load_listing(token):
    """webhook id -> guild id across every guild the heartbeat bot is in, plus a
    per-guild count. Raises if any guild can't be listed: an incomplete listing
    would make live webhooks look absent."""
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    listing, counts = {}, {}
    async with aiohttp.ClientSession(headers=headers) as http:
        for guild in await _discord_get(http, "/users/@me/guilds"):
            hooks = await _discord_get(http, f"/guilds/{guild['id']}/webhooks")
            counts[guild["name"]] = len(hooks)
            for hook in hooks:
                listing[str(hook["id"])] = str(guild["id"])
    return listing, counts


async def probe_all(urls):
    verdicts, answers = {}, Counter()
    queue = asyncio.Queue()
    for url in dict.fromkeys(urls):
        queue.put_nowait(url)
    async with aiohttp.ClientSession() as http:
        async def worker():
            while not queue.empty():
                url = queue.get_nowait()
                verdicts[url], detail = await probe_webhook(http, url)
                answers[detail] += 1
                await asyncio.sleep(PROBE_SPACING_SECONDS)

        await asyncio.gather(*(worker() for _ in range(PROBE_WORKERS)))
    return verdicts, answers


def apply_plan(defuse, restore, purge):
    from db.models import Session, Webhook, WebhookPendingDeletion

    with Session() as s:
        if defuse:
            gone = s.query(WebhookPendingDeletion).filter(
                WebhookPendingDeletion.id.in_([row[0] for row in defuse]),
                (WebhookPendingDeletion.webhook_url.is_(None)) | (WebhookPendingDeletion.webhook_url == ""),
            ).delete(synchronize_session=False)
            s.commit()
            print(f"defuse:  deleted {gone} pending row(s)")
        if restore:
            added = 0
            for webhook_id, url, webhook_type in restore:
                exists = s.query(Webhook.id).filter(
                    (Webhook.webhook_id == webhook_id) | (Webhook.webhook_url == url)).first()
                if not exists:
                    s.add(Webhook(webhook_id=webhook_id, webhook_url=url, type=webhook_type))
                    added += 1
            s.commit()
            print(f"restore: added {added} webhook(s)")
        if purge:
            gone = 0
            for row in purge:
                gone += s.query(Webhook).filter(
                    Webhook.id == row.id, Webhook.webhook_url == row.url).delete(synchronize_session=False)
            s.commit()
            print(f"purge:   deleted {gone} row(s)")


# --- main --------------------------------------------------------------------------

async def run(args):
    steps = set(args.steps)
    rows, types, pending = load_table()
    print(f"Webhook pool repair — {'APPLY' if args.apply else 'DRY RUN (nothing written; re-run with --apply)'}")
    print(f"Table: {len(rows)} webhooks row(s), {len(pending)} webhook_pending_deletion row(s)")

    defuse = plan_defuse(pending) if "defuse" in steps else []

    listing, counts = {}, {}
    if steps & {"restore", "purge"}:
        listing, counts = await load_listing(os.getenv("HEARTBEAT_BOT_TOKEN"))
        print(f"Guild webhook listings: {counts}")

    snapshot = load_snapshot(args.restore_ref) if "restore" in steps else {}
    urls = list(snapshot.values()) if "restore" in steps else []
    if "purge" in steps:
        urls += [row.url for row in rows if row.webhook_id not in listing]
    verdicts, answers = await probe_all(urls)
    inconclusive = sum(1 for verdict in verdicts.values() if verdict == INCONCLUSIVE)
    print(f"Probes: {len(verdicts)} (answers {dict(answers)}; inconclusive {inconclusive})")
    print()

    if "defuse" in steps:
        print(f"defuse:  {len(defuse)} pending row(s) with no URL"
              + "".join(f"\n           row {r[0]}: webhook {r[1]}, channel {r[3]}" for r in defuse))

    restore = []
    if "restore" in steps:
        restore, skipped = plan_restore(snapshot, rows, listing, verdicts)
        print(f"restore: {len(restore)} of {len(snapshot)} snapshot webhook(s) from ref {args.restore_ref[:7]} "
              f"— by type {dict(Counter(t for _, _, t in restore))}; skipped {dict(skipped)}")

    purge = []
    if "purge" in steps:
        purge, kept = plan_purge(rows, listing, verdicts)
        refusal = purge_refusal(len(verdicts), inconclusive, len(purge), len(rows), args.max_purge_ratio)
        print(f"purge:   {len(purge)} row(s) absent from every listing and answering Unknown Webhook "
              f"— by type {dict(Counter(types.get(row.id) for row in purge))}; "
              f"absent but kept {dict(kept)}")
        if refusal:
            print(f"         REFUSED: {refusal}")
            purge = []

    net = len(rows) + len(restore) - len(purge)
    print(f"\nwebhooks rows after apply: {len(rows)} + {len(restore)} - {len(purge)} = {net}")
    if args.apply:
        apply_plan(defuse, restore, purge)


def main():
    load_dotenv(os.path.join(REPO_ROOT, ".env"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry-run)")
    parser.add_argument("--steps", nargs="+", choices=STEPS, default=list(STEPS))
    parser.add_argument("--restore-ref", default=DEFAULT_RESTORE_REF, help="pages repo commit to restore from")
    parser.add_argument("--max-purge-ratio", type=float, default=0.5,
                        help="refuse to purge more than this fraction of the table (default 0.5)")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
