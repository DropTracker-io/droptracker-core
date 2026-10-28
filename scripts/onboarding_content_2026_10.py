"""Onboarding content pass (2026-10-28): the database half.

The code half (bot copy, setup wizards, feature catalog) ships in git. These
are the rows only the database holds:

* **redirects**: dead links that old plugin versions and old bot messages
  still point at (``/wiki/why-api``, ``/wiki/create-group``, ``/forums/...``,
  ``/login``). New code no longer links them, but clients in the wild do.
* **docs pages** (the DB is canonical for docs): ``create-group`` and
  ``link-account`` described the 2024 flow (``/create-group`` first, claim only
  in Discord); ``getting-started`` and ``runelite-plugin`` named the wrong
  plugin setting ("Use API Connections" is "Use API", under Advanced).
* **Task generation from text** (2026-10-28 follow-up): new daily limits on
  every group plan (Free 25, Supporter 100, Sponsor 250, Patron 500; they were
  5/25/75/200), and the plan cards call it "Generate event tasks from plain
  text" rather than "Build event tasks by describing them".
* **Free plan card**: one more line, so the free features the bot's "What is
  DropTracker?" explainer and the /premium page list (both read this row)
  include the Clan Log, recaps, the clan chat bridge and the Activity.

Dry-run by default; ``--apply`` writes. Idempotent: rows already in the target
state are skipped. Docs bodies are backed up to ``--backup-dir`` before an
overwrite.

    venv/bin/python -m scripts.onboarding_content_2026_10            # dry run
    venv/bin/python -m scripts.onboarding_content_2026_10 --apply
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

REDIRECTS = [
    # (source, destination, permanent, forward_query)
    ("/wiki/why-api", "/docs/runelite-plugin#api-connections-recommended", True, False),
    ("/wiki/create-group", "/docs/create-group", True, False),
    ("/forums/suggestions", "/suggestions", True, True),
    ("/forums/bug-reports", "/tickets", True, True),
    ("/login", "/dashboard", False, True),
]
REDIRECT_NOTE = "2026-10-28 onboarding review: dead link in old plugin/bot copy"

#: tier key -> ai_task_gen_daily (stored on subscription_tiers.entitlements).
TASK_GEN_LIMITS = {"free": 25, "basic": 100, "t2": 250, "t3": 500}
TASK_GEN_OLD_PREFIX = "Build event tasks by describing them:"


def task_gen_line(limit: int) -> str:
    return f"Generate event tasks from plain text: {limit} a day"


FREE_TIER_LINE = "**Clan Log**, monthly recaps, the clan chat bridge and the Discord Activity"

CREATE_GROUP = """# Creating a group

A *group* connects your Discord server, our Discord bot and a
[Wise Old Man](https://wiseoldman.net) group. Members are added and removed
from your Wise Old Man group automatically, and their drops post to your
Discord. Everything here is free.

## Before you start

- A [Wise Old Man group](https://wiseoldman.net/groups) for your clan. No
  group yet? [Create one](https://wiseoldman.net/groups/create) first.
- **Manage Server** permission in your clan's Discord server.

## On the website (recommended)

The [setup wizard](/groups/new) takes about two minutes:

1. **Discord server**: pick your server. If the bot isn't in it yet, the
   wizard invites it and waits for it to arrive.
2. **Wise Old Man group**: paste its id or its page URL.
3. **Name**: this creates the group and starts syncing members.
4. **Channels**: where drop notifications and the lootboard post. Nothing
   posts to Discord until you pick a drops channel.
5. **Turn things on**: clan points, the Clan Log, monthly recaps, death
   announcements, a submit button and more, one click each.
6. **Finish**: post a short guide for your members in Discord.

## In Discord

Add the bot to your server and it posts a welcome card with a **Set up
DropTracker** button. You can also run **`/group-setup`** at any time. It walks
you through the same steps without leaving Discord, and later it's where you
change settings.

## In game

In the plugin's **Group** tab, **Register your group** does it from RuneLite:
you sign in with Discord and add the bot in one go.

## After setup

Your group's **Manage** page (from your [dashboard](/dashboard)) has a setup
checklist and a list of features you haven't tried yet. See
[configuring your group](/docs/group-settings) for every setting.

Each Discord server can have one group, and a Wise Old Man group can only be
registered once.
"""

LINK_ACCOUNT = """# Linking your OSRS account

Your account is created automatically the first time the
[plugin](/docs/runelite-plugin) tracks something for it. **Claiming** it links
it to you, so your drops ping you in Discord and show on your profile.

## On the website

1. [Sign in with Discord](/register).
2. Type your in-game name, exactly as it appears, and press **Claim**.

## In Discord

Run **`/claim-rsn`** with your in-game name in any server with the DropTracker
bot. In your clan's server, you're also added to that clan.

## Managing your accounts

- Your [dashboard](/dashboard) lists every account you've claimed, with a
  **Customize** button for its profile.
- **`/accounts`** lists them in Discord, and **`/unclaim-rsn`** removes one.
- You can claim as many accounts as you like.

## Troubleshooting

- **"Player not found"**: the plugin hasn't tracked anything for that account
  yet. Get some loot or an achievement with the plugin on, then try again.
- **Claimed, but drops don't post in your clan's channel**: your clan builds
  its member list from its Wise Old Man group. Ask a leader to add you there;
  it syncs every hour.
- **"Somebody else claimed this account"**: if that's a mistake, reach out in
  [our Discord](https://discord.gg/droptracker) and we'll sort it out.
"""

GETTING_STARTED_EDITS = [
    (
        """1. **Install the DropTracker plugin** from the RuneLite Plugin Hub and play —
   your account is created automatically the first time the plugin submits
   something for it.
2. **Claim your account** with the `/claim-rsn` Discord command so it's tied
   to your Discord identity. See [linking your account](/docs/link-account).""",
        """1. **Install the DropTracker plugin** from the RuneLite Plugin Hub and play.
   Your account is created automatically the first time the plugin tracks
   something for it. Optionally, turn on **Use API** in its settings (under
   Advanced) for live boards and in-game event updates.
2. **Claim your account** [on the website](/register) or with `/claim-rsn` in
   Discord, so it's tied to you. See [linking your account](/docs/link-account).""",
    ),
]

RUNELITE_PLUGIN_EDITS = [
    (
        "In the plugin's **API Configuration** section, enable **Use API Connections**.",
        "In the plugin's **Advanced** section, turn on **Use API**.",
    ),
    (
        """- **Event participation** — the API is required to take part in
  [events](/docs/events-players).""",
        """- Your [events](/docs/events-players) in game: progress in the side panel,
  pop-ups and team indicators. Events count your submissions either way.""",
    ),
]

DOC_DESCRIPTIONS = {
    "create-group": "Set up your clan in about two minutes, on the website, in Discord or in game.",
    "link-account": "Claim your OSRS account on the website or with /claim-rsn.",
}


def _plan_redirects(s, SiteRedirect):
    existing = {r.source: r for r in s.query(SiteRedirect).all()}
    top = max([r.order or 0 for r in existing.values()] or [0])
    plan = []
    for i, (src, dst, permanent, fq) in enumerate(REDIRECTS):
        row = existing.get(src)
        if row is not None:
            print(f"  redirect {src}: exists -> {row.destination} (skip)")
            continue
        print(f"  redirect {src} -> {dst}")
        plan.append(SiteRedirect(source=src, destination=dst, permanent=permanent, enabled=True,
                                 order=top + 1 + i, forward_query=fq, note=REDIRECT_NOTE))
    return plan


def _apply_edits(body: str, edits) -> tuple[str, int]:
    done = 0
    for old, new in edits:
        if old in body:
            body = body.replace(old, new)
            done += 1
    return body, done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--backup-dir", default=os.path.expanduser("~/backups"),
                    help="where the old docs bodies are saved before an overwrite")
    args = ap.parse_args(argv)

    from db.models import Session
    from db.models.subscriptions import SubscriptionTier
    from db.models.web import DocsPage, SiteRedirect

    s = Session()
    try:
        print("Redirects:")
        new_redirects = _plan_redirects(s, SiteRedirect)

        print("Docs pages:")
        backups = {}
        docs = {d.slug: d for d in s.query(DocsPage).filter(
            DocsPage.slug.in_(["create-group", "link-account", "getting-started", "runelite-plugin"]))}
        for slug, body in (("create-group", CREATE_GROUP), ("link-account", LINK_ACCOUNT)):
            d = docs.get(slug)
            if d is None:
                print(f"  {slug}: missing (skip)")
                continue
            if d.body_md.strip() == body.strip():
                print(f"  {slug}: already current (skip)")
                continue
            backups[slug] = {"body_md": d.body_md, "description": d.description}
            print(f"  {slug}: rewrite ({len(d.body_md)} -> {len(body)} chars)")
            if args.apply:
                d.body_md = body
                d.description = DOC_DESCRIPTIONS[slug]
        for slug, edits in (("getting-started", GETTING_STARTED_EDITS), ("runelite-plugin", RUNELITE_PLUGIN_EDITS)):
            d = docs.get(slug)
            if d is None:
                print(f"  {slug}: missing (skip)")
                continue
            body, n = _apply_edits(d.body_md, edits)
            print(f"  {slug}: {n}/{len(edits)} edits apply")
            if n and args.apply:
                backups[slug] = {"body_md": d.body_md, "description": d.description}
                d.body_md = body

        print("Free plan card:")
        tier = s.query(SubscriptionTier).filter(SubscriptionTier.key == "free").first()
        if tier is None:
            print("  no 'free' tier row (skip)")
        else:
            lines = json.loads(tier.features) if tier.features else []
            if FREE_TIER_LINE in lines:
                print("  line already present (skip)")
            else:
                print(f"  add: {FREE_TIER_LINE}")
                if args.apply:
                    # After the clan points line: notifications, designs,
                    # points, then this, then events and the lootboard.
                    at = 3 if len(lines) >= 3 else len(lines)
                    lines.insert(at, FREE_TIER_LINE)
                    tier.features = json.dumps(lines)

        print("Task generation limits:")
        for tier_key, limit in TASK_GEN_LIMITS.items():
            t = s.query(SubscriptionTier).filter(SubscriptionTier.key == tier_key).first()
            if t is None:
                print(f"  {tier_key}: no tier row (skip)")
                continue
            ents = json.loads(t.entitlements) if t.entitlements else {}
            lines = json.loads(t.features) if t.features else []
            new_lines = [task_gen_line(limit) if str(l).startswith(TASK_GEN_OLD_PREFIX)
                         or str(l).startswith("Generate event tasks from plain text:") else l
                         for l in lines]
            before = ents.get("ai_task_gen_daily")
            if before == limit and new_lines == lines:
                print(f"  {tier_key}: already {limit} (skip)")
                continue
            print(f"  {tier_key}: {before} -> {limit} a day; card line updated: {new_lines != lines}")
            if args.apply:
                ents["ai_task_gen_daily"] = limit
                t.entitlements = json.dumps(ents)
                t.features = json.dumps(new_lines)

        if not args.apply:
            print("\nDry run. Re-run with --apply to write.")
            s.rollback()
            return 0

        if backups:
            os.makedirs(args.backup_dir, exist_ok=True)
            path = os.path.join(args.backup_dir,
                                f"docs-onboarding-{datetime.now():%Y%m%d-%H%M%S}.json")
            with open(path, "w") as fh:
                json.dump(backups, fh, indent=2)
            print(f"Backed up old docs to {path}")
        for row in new_redirects:
            s.add(row)
        s.commit()
        try:
            from db.entitlements import invalidate_entitlement_cache

            invalidate_entitlement_cache()
        except Exception as e:  # services reload it within their cache TTL anyway
            print(f"(entitlement cache not invalidated here: {e})")
        print("Applied.")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
