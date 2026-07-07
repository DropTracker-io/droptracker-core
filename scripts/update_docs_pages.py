"""Rewrite ``docs_pages`` content to match the live application (2026-07).

The original 9 pages (seeded by ``scripts.seed_docs_pages`` from the old
static ``.mdx`` files) described flows that no longer match reality — most
notably a plugin-code account claim that was never built (the real flow is
the Discord ``/claim-rsn`` command) — and predate events v2, badges,
deaths/diaries notifications, manual submissions, and custom embeds.

This script **overwrites** the body/metadata of every slug listed in PAGES
and creates the ones that don't exist yet. Content is otherwise edited
through ``/admin/docs``; re-running this script will clobber CMS edits to
these slugs, so treat it as a one-time content drop.

Run:
    venv/bin/python -m scripts.update_docs_pages            # apply
    venv/bin/python -m scripts.update_docs_pages --dry-run  # preview
"""
from __future__ import annotations

import argparse
import sys

from db.models import DocsPage, session

BOT_INVITE = "https://discord.com/oauth2/authorize?client_id=1172933457010245762"
DISCORD = "https://www.droptracker.io/discord"

# slug -> (title, description, category, order, body_md)
PAGES: dict[str, tuple[str, str, str, int, str]] = {

    # ------------------------------------------------------------------
    # Getting started
    # ------------------------------------------------------------------
    "getting-started": (
        "Getting started",
        "What DropTracker is and how to start tracking your loot.",
        "Getting started", 1, f"""
# Getting started

DropTracker records Old School RuneScape drops, collection log slots, personal
bests, combat achievements, pets, quests, level-ups, and more — then ranks
players and clans on real-time [leaderboards](/leaderboards), posts
notifications to your clan's Discord, and renders shareable lootboards.

There are two ways loot gets in:

1. **The RuneLite plugin** (recommended) automatically submits your drops and
   achievements as you play. See [the plugin guide](/docs/runelite-plugin).
2. **Manual submission** on the website for one-off entries with screenshot or
   video proof. See [manual submissions](/docs/manual-submissions).

## In three steps

1. **Install the DropTracker plugin** from the RuneLite Plugin Hub and play —
   your account is created automatically the first time the plugin submits
   something for it.
2. **Claim your account** with the `/claim-rsn` Discord command so it's tied
   to your Discord identity. See [linking your account](/docs/link-account).
3. **Join or create a group** to compete on clan leaderboards and unlock
   Discord notifications for your clan. See
   [creating a group](/docs/create-group).

Once you're set up, [sign in with Discord](/api/auth/login) to see your
accounts and groups on your [dashboard](/dashboard), and check the
[leaderboards](/leaderboards) to see where you rank.
""",
    ),

    "how-it-works": (
        "How it works",
        "The path a drop takes from the game to the leaderboards.",
        "Getting started", 2, """
# How it works

When you receive a drop in-game, here's what happens:

1. **The RuneLite plugin** detects it and submits it to DropTracker.
2. **The intake pipeline** verifies the account against
   [Wise Old Man](https://www.wiseoldman.net), values the item using live
   Grand Exchange prices, filters duplicates, and records it. High-value drops
   get an extra verification pass to keep leaderboards honest.
3. **Leaderboards update** — daily, weekly, monthly, and all-time boards, both
   global and per-group.
4. **Notifications** are posted to your clan's configured Discord channels,
   subject to that group's [settings](/docs/group-settings) (minimum value,
   screenshot requirements, and so on).
5. **The website** reflects the change in real time — the live drop feed,
   leaderboards, and profiles update without a refresh.

## Identity

DropTracker is Discord-native. Your **Discord account** is your identity on
the site, and each **OSRS account** you play is linked to it once you
[claim it](/docs/link-account). A single Discord user can own several OSRS
accounts, and all of them roll up to your profile.

## Groups

A **group** is a clan: a Wise Old Man group tied to a Discord server.
Membership is synced from Wise Old Man and drives the clan leaderboards,
lootboards, and who notifications are sent for. See
[creating a group](/docs/create-group).
""",
    ),

    "runelite-plugin": (
        "The RuneLite plugin",
        "Install and configure the DropTracker plugin for automatic tracking.",
        "Getting started", 3, """
# The RuneLite plugin

The plugin is how loot gets tracked automatically. Install it once, and your
drops and achievements are submitted as you play.

## Installing

1. Open RuneLite and click the **wrench icon** (Configuration).
2. Open the **Plugin Hub** and search for **DropTracker**.
3. Click **Install**, then make sure the plugin is enabled.

That's it — the next drop or achievement you receive creates your account in
our database, ready to be [claimed](/docs/link-account).

## What it tracks

Each category can be toggled independently in the plugin settings, with a
matching option to attach a screenshot:

- **Drops** — with a configurable minimum value for screenshots.
- **Collection log** slots.
- **Personal bests** — boss kill times, including raids.
- **Combat achievements.**
- **Pets.**
- **Levels / experience** — with a minimum level for screenshots.
- **Quests.**
- **Deaths.**
- **Achievement diaries.**

## API connections (recommended)

In the plugin's **API Configuration** section, enable **Use API Connections**.
This lets the plugin talk to the DropTracker database directly, which gives
you:

- The most accurate and robust tracking.
- Live data in the plugin's side panel.
- **Event participation** — the API is required to take part in
  [events](/docs/events).

RuneLite shows a standard warning when enabling third-party API connections —
that's expected for any Plugin Hub plugin that talks to an external server.

## Other options

- **Capture mode** — choose screenshot quality, or short video clips instead
  of stills where supported.
- **Hide PMs** — hides private messages before screenshots are captured.
- **Receive in-game messages** — get confirmations in your chatbox.

## Not seeing your submissions?

Check that the plugin is enabled, the category you expect is toggled on, and
you're logged into the account in question. Still stuck? Ask in
[our Discord](https://www.droptracker.io/discord).
""",
    ),

    "manual-submissions": (
        "Manual submissions",
        "Submit a drop or achievement from the website with proof attached.",
        "Getting started", 4, """
# Manual submissions

Sometimes the plugin isn't running when something good happens. The
[submit page](/submit) lets you record it yourself.

## Requirements

- [Sign in with Discord](/api/auth/login).
- At least one [claimed OSRS account](/docs/link-account).

## Submitting

1. Go to [Submit a drop](/submit).
2. Pick the **submission type** — drop, collection log, personal best, combat
   achievement, or pet.
3. Choose which of your **accounts** it belongs to, and fill in the source
   (NPC/boss), item, value, and quantity as applicable.
4. Optionally attach **proof** — a screenshot or video clip uploads directly
   with your submission. Some groups require an image before a notification
   is posted, so proof is worth including.

## When to use it

Manual submission is for one-offs — the plugin is always preferred because
it's automatic and verified as you play. If you find yourself manually
submitting often, check the [plugin's settings](/docs/runelite-plugin) to make
sure the category is enabled.
""",
    ),

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    "link-account": (
        "Linking your account",
        "Claim an OSRS account with /claim-rsn so its loot appears on your profile.",
        "Account", 1, f"""
# Linking your OSRS account

OSRS accounts are created automatically the first time the
[plugin](/docs/runelite-plugin) submits something for them. To attach an
account to your Discord identity, you **claim** it with the `/claim-rsn`
Discord command.

## Steps

1. **Install the plugin** and receive a drop or achievement with it enabled —
   this creates the account in our database.
2. In a Discord server with the DropTracker bot — your clan's server, or
   [our community server]({DISCORD}) — run **`/claim-rsn`** with your in-game
   name, exactly as it appears.
3. Done. If you ran the command in your clan's server, you're also added to
   that group automatically.

[Sign in with Discord](/api/auth/login) on the site and your claimed accounts
appear on your [dashboard](/dashboard), with their loot, ranks, and
submissions rolled up to your profile.

## Managing your accounts

- **`/accounts`** — list the accounts you've claimed.
- **`/unclaim-rsn`** — remove an account from your Discord.
- You can claim as many accounts as you like — they all count toward your
  groups.

## Troubleshooting

- **"Player not found"** — the account hasn't submitted anything yet. Install
  the plugin, receive some loot or an achievement with it enabled, then try
  again.
- **"Somebody else claimed this account"** — if that's a mistake, reach out in
  [our Discord]({DISCORD}) and we'll sort it out.
""",
    ),

    "privacy-and-notifications": (
        "Privacy & notifications",
        "Control who can see your loot and when you get pinged.",
        "Account", 2, """
# Privacy & notifications

Manage these from [Settings](/settings) after signing in.

## Privacy

- **Public profile** — show your profile and loot on the site. Turn this off
  to keep your stats private.
- **Hidden** — remove yourself from leaderboards entirely.

## Pings

- **Global pings** — allow `@`-mentions in the global server.
- **Group pings** — allow `@`-mentions in your clans.
- **Never ping** — overrides the above; you are never `@`-mentioned anywhere.

## Direct messages

- **DM on rank change** — get a DM when your global rank changes.
- **DM on points** — get a DM when you earn or spend points.
- **Update logs** — receive DropTracker product updates.

## Premium group preferences

If you support DropTracker via Patreon or a group subscription, choose which
group your **Patreon** and **premium** benefits apply to.

## Appearance

The settings page also has a **site theme** picker — the choice is stored on
your device and applied instantly.
""",
    ),

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    "create-group": (
        "Creating a group",
        "Link a Wise Old Man group and a Discord server to start a clan.",
        "Groups", 1, f"""
# Creating a group

A *group* connects your Discord server, our Discord bot, and a
[Wise Old Man](https://www.wiseoldman.net) group — so your clan members are
automatically added, removed, and tracked inside your DropTracker group.

## Before you start

- The **[DropTracker Discord bot (click to invite)]({BOT_INVITE})** must be in
  your Discord server.
- You need **Administrator** permission in that server.
- Your group must already exist on Wise Old Man, and you'll need its ID
  (generally 3–6 digits, e.g. `25637`).

## Creating your group

1. In **your own Discord server**, run the **`/create-group`** command with
   your desired group name (publicly displayed) and your Wise Old Man group
   ID.
2. The bot confirms the group was created and walks you through the basics.
   Each Discord server can own one group, and a Wise Old Man group can only be
   registered once.
3. [Sign in with Discord](/api/auth/login) on the site, open your
   [dashboard](/dashboard), and press **Manage** under your new group.

From there, configure the group to your liking — see
[configuring your group](/docs/group-settings).

## Prefer to do it on the website?

The [create a group](/groups/new) wizard does the same thing from the
browser: look up your Wise Old Man group by ID, enter your Discord server ID
(the wizard checks the bot is present), confirm the name, and you're taken
straight to your group's admin dashboard.
""",
    ),

    "group-settings": (
        "Configuring your group",
        "Notification channels, thresholds, lootboards, and members.",
        "Groups", 2, """
# Configuring your group

Group owners and admins manage everything from the group's **Manage** area on
the website — open your [dashboard](/dashboard) and press **Manage** on the
group.

## Notification channels

On the **Settings** tab, pick a Discord channel for each notification type —
drops, levels, personal bests, combat achievements, pets, quests, collection
log slots, deaths, and achievement diaries. Leave a channel unset to disable
that notification type. The channel picker lists your server's channels
directly, so there's no copying IDs around.

## Drop notifications

- **Minimum value to notify** — suppress drop announcements below a GP value.
- **Only send with images** — require a screenshot before posting.
- **Send stacks of items** — announce stackable drops (e.g. runes) too.
- Per-category toggles — collection log, combat achievements, pets, quests,
  levels, personal bests, and the newer **deaths** and **diaries** categories
  (both off by default).

## Personal bests & Hall of Fame

Choose which bosses appear, how many personal bests are displayed, and where
PB embeds are posted. Groups with the Hall of Fame feature
([Sponsor tier and up](/docs/premium)) configure their boss list here too.

## Lootboards

Pick the board style, the channel it's posted to, and whether the bot reposts
the image or edits the existing message. See [lootboards](/docs/lootboards).

## Seasonal settings

Most notification keys have a **seasonal** mirror, so you can run a parallel
configuration for Leagues / seasonal worlds without touching your main setup.

## The other tabs

- **Members** — sync membership from Wise Old Man and hide or unhide
  individual players from the group's boards.
- **Announcements** — post announcements to your group's page on the site.
- **Events** — create and run [events](/docs/events).
- **Embeds** — customize your notification embeds
  ([Sponsor tier and up](/docs/custom-embeds)).
- **Subscription** — manage your [premium subscription](/docs/premium).
- **Diagnostics** — check that the bot, channels, and permissions are healthy.
""",
    ),

    "lootboards": (
        "Lootboards",
        "Your group's loot as a live board on the web and in Discord.",
        "Groups", 3, """
# Lootboards

A **lootboard** shows a group's loot as a grid of item tiles. It comes in two
forms:

## The live web board

Open it from any group page via the **Lootboard** button. It's rendered live
from the latest data:

- Tiles are **colored by value** — the rarer the drop, the richer the color.
- **Hover** a tile to see the item name, quantity, and total value.
- Use the **period switcher** to view all-time, monthly, weekly, or daily
  loot.
- The **Download image** button generates a shareable PNG of the board.

## The Discord board

The bot posts your group's lootboard image to the channel configured in your
[group settings](/docs/group-settings) and refreshes it automatically every
couple of minutes. You can choose the board's visual theme, and whether the
bot **edits the existing message** in place or **reposts** a fresh one each
time.
""",
    ),

    "custom-embeds": (
        "Custom Discord embeds",
        "Customize the notification embeds the bot posts for your group.",
        "Groups", 4, """
# Custom Discord embeds

Groups on the **Sponsor** tier and above ([see premium](/docs/premium)) can
fully customize the Discord embeds the bot posts — for drops, collection log
slots, personal bests, combat achievements, pets, level-ups, quests, and the
lootboard post itself.

## Editing your embeds

Open your group's **Embeds** tab from the Manage area. For each embed type you
can set:

- **Title** and **description**.
- **Color** (hex, e.g. `#ffb83f`), **thumbnail**, and **banner image**.
- Any number of **fields** (name + value pairs).

## Placeholders

Placeholders are filled in when each notification is sent — for example
`{player_name}`, `{item_name}`, and `{npc_name}`. The editor lists every
placeholder available for the embed type you're editing; click one to copy
it, then paste it into the title, description, or any field.

A **live preview** shows your embed with sample data as you type, and each
embed type can be reverted to the default at any time.
""",
    ),

    "premium": (
        "Premium subscriptions",
        "Upgrade a group with a recurring subscription.",
        "Groups", 5, """
# Premium subscriptions

Groups can subscribe to a recurring premium tier that unlocks extra features.
Compare plans and current pricing on the [premium page](/premium).

## Tiers

- **Supporter** — support the project and stand out: a badge on the site and
  a Discord role.
- **Sponsor** — everything above, plus the **Hall of Fame** extension,
  [custom Discord embeds](/docs/custom-embeds), and access to a customizable
  point system for running competitions.
- **Patron** — everything above, plus the [events system](/docs/events)
  (bingo, task races, and more) and video capture alongside notifications
  instead of just images.

## Managing a subscription

From the group's **Subscription** tab, an owner or admin can:

- **Subscribe** to a tier through a secure hosted checkout, or **switch**
  between tiers.
- **Cancel** — the group keeps its benefits until the end of the paid period.
- **Resume** a subscription that's set to cancel.
- **Manage billing** — open the payment provider's portal to update a card or
  view invoices.

Billing is handled by our payment provider; DropTracker never stores card
details.
""",
    ),

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    "events": (
        "Events",
        "Join or run clan events — task races, bingo boards, and teams.",
        "Events", 1, """
# Events

Events are group-run competitions: a set of **tasks** to complete, optionally
laid out as a **bingo board**, played solo or in **teams**. Progress is
tracked automatically from your in-game submissions.

## Joining an event (players)

1. Browse [events](/events) or open one from your group's page.
2. [Sign in with Discord](/api/auth/login) — you'll need at least one
   [claimed OSRS account](/docs/link-account).
3. Join from the event page. Depending on how the event is set up, you either
   pick a team yourself (possibly with a **join code** from your clan),
   get assigned automatically, or are placed by an admin.

> **Important:** enable **Use API Connections** in the
> [RuneLite plugin](/docs/runelite-plugin) — it's required for your drops and
> achievements to count toward event tasks.

Once the event is live, the event page shows the board, teams, and progress in
real time. Some tasks are verified automatically from your submissions; others
need an admin to confirm them.

## Running an event (group admins)

Events are available to groups on the **Patron** tier
([see premium](/docs/premium)), with one active event at a time.

From your group's **Events** tab you can:

- **Create an event** — name, description, dates, and how teams are formed
  (self-join with an optional join code, automatic assignment, or
  admin-assigned).
- **Build the task list** — pick tasks from the shared task library or write
  your own, with per-task points and an optional "requires confirmation" flag
  for things that can't be verified automatically.
- **Design a bingo board** — arrange tasks on a grid with the board designer.
- **Manage teams and participants** — create teams, move players, and track
  standings.
- **Review submissions** — a review queue holds task completions that need
  manual confirmation.
- **Connect Discord channels** — route event announcements and progress
  updates to channels in your server.

Events move through a simple lifecycle: **draft** while you build, **active**
while it runs, and **past** when it ends.
""",
    ),

    # ------------------------------------------------------------------
    # Reference
    # ------------------------------------------------------------------
    "badges": (
        "Badges",
        "Profile badges and how to earn them.",
        "Reference", 1, """
# Badges

Badges are awarded automatically and displayed on your player profile.

## Current badges

- **Daily Loot Champion** — received the most loot of any tracked player on a
  calendar day.
- **Week-Long Grinder** — logged loot every day for 7 days in a row.
- **Iron Discipline** — logged loot every day for 30 days in a row.
- **Boss Record Holder** — holds the fastest tracked kill time at a boss and
  team size. This one is *held*, not permanent — beat the record and you take
  the badge; lose it and it moves on.
- **Bug Tester** — awarded by the team to users who help test new features
  and report bugs.

Badges are evaluated daily, so a new streak or record can take up to a day to
appear on your profile.
""",
    ),

    "faq": (
        "FAQ",
        "Common questions about DropTracker.",
        "Reference", 2, f"""
# Frequently asked questions

## My drop didn't show up in Discord — why?

Most often the value was below your group's **minimum value to notify**, the
group requires a **screenshot**, or the item was a stack and the group has
stackable announcements off. Check the group's
[settings](/docs/group-settings). Your personal totals and leaderboards still
update even when a notification is suppressed.

## `/claim-rsn` says "Player not found" — what now?

The account has to submit something first. Install the
[RuneLite plugin](/docs/runelite-plugin), receive some loot or an achievement
with it enabled, then run `/claim-rsn` again.

## Someone else claimed my account name!

Reach out in [our Discord]({DISCORD}) and we'll investigate and fix the
ownership.

## Can I track more than one account?

Yes. Claim as many OSRS accounts as you like with `/claim-rsn` — each one
shows on your [dashboard](/dashboard), and all of them count toward your
groups.

## How are leaderboards calculated?

Loot is valued with live Grand Exchange prices and aggregated into daily,
weekly, monthly, and all-time boards — globally and per group — updated in
real time. Players who set themselves to hidden are excluded.

## My event progress isn't counting.

Make sure **Use API Connections** is enabled in the
[plugin's settings](/docs/runelite-plugin) — it's required for
[events](/docs/events). Also check that the task's category (drops, clogs,
etc.) is enabled in the plugin.

## My group's members are out of date.

Membership syncs from [Wise Old Man](https://www.wiseoldman.net). Update your
WOM group, then sync from the group's **Members** tab (or ask an admin to).

## Is my data private?

You control visibility from [Settings](/settings) — keep your profile private
or hide from leaderboards entirely. See
[privacy & notifications](/docs/privacy-and-notifications).
""",
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Rewrite docs_pages content (2026-07 overhaul).")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = ap.parse_args()

    created = updated = 0
    for slug, (title, description, category, order, body) in PAGES.items():
        body = body.strip() + "\n"
        row = session.query(DocsPage).filter(DocsPage.slug == slug).first()
        action = "update" if row else "create"
        print(f"  {'would ' if args.dry_run else ''}{action}  {slug:28} ({category}, order={order}, {len(body)} chars)")
        if args.dry_run:
            continue
        if row:
            row.title, row.description = title, description
            row.category, row.order, row.body_md = category, order, body
            updated += 1
        else:
            session.add(DocsPage(
                slug=slug, title=title, description=description,
                category=category, order=order, body_md=body,
            ))
            created += 1

    if not args.dry_run:
        session.commit()
    print(f"\n{'Previewed' if args.dry_run else 'Done'}: {created} created, {updated} updated, {len(PAGES)} total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
