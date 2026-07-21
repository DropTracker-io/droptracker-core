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

## Review before it counts

During **events**, manual submissions usually don't score instantly: most
events are set to hold anything that didn't come from the plugin in a
**pending review queue**, and an event admin confirms it before points are
awarded. If your submission shows as *pending*, that's working as intended —
it will count as soon as an admin approves it. (Event organizers can change
this under the event's submission policy.)

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
[group settings](/docs/group-settings) and refreshes it automatically —
**hourly** for standard groups, and every few minutes for groups with a
[premium subscription](/premium). You can choose the board's visual theme,
and whether the bot **edits the existing message** in place or **reposts** a
fresh one each time.
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
        "Events overview",
        "Join or run clan events — task races, bingo boards, and teams.",
        "Events", 1, """
# Events

Events are group-run competitions built from **tasks** — objectives to complete
in-game, like collecting an item, killing a boss a number of times, or reaching
an XP goal. Tasks can be arranged on a **bingo board**, played solo or in
**teams**, and progress is tracked **automatically** from your in-game
submissions.

Whether you're joining your clan's next bingo or building one from scratch,
these pages walk through the whole system.

## For players

- **[Joining & playing an event](/docs/events-players)** — find an event, get on
  a team, and make sure your progress counts.

## For group leaders

- **[Creating & running an event](/docs/events-create)** — the create form,
  event settings, and the draft → active → past lifecycle.
- **[Building tasks](/docs/events-tasks)** — every task type, the drag-and-drop
  item/NPC builder, points, and review rules.
- **[Bingo boards](/docs/events-bingo)** — the board designer, line and blackout
  bonuses, and free cells.
- **[Teams, sign-ups & clan vs clan](/docs/events-teams)** — how players end up
  on teams, the sign-up pool, and challenging another clan.
- **[Reviewing, scoring & Discord](/docs/events-review)** — the review queue,
  manual awards, and routing announcements to Discord.
- **[Reusing your work](/docs/events-templates)** — the shared task library and
  saving an event as a reusable template.

## Key ideas

- **Task** — one objective, worth some **points**. Most complete automatically;
  a few are marked for manual review or awarded by an admin.
- **Board** — an optional bingo grid of tasks; completing lines or the whole
  board can award bonus points.
- **Team** — everyone competes as a team, even a team of one. A team's score is
  the points of everything it has completed.
- **Lifecycle** — an event is a **draft** while you build it, **active** while it
  runs, and **past** once it ends.

> Running events requires the **Patron** tier ([see premium](/docs/premium)).
> Joining one is free for any player with a claimed account.
""",
    ),

    "events-players": (
        "Joining & playing an event",
        "Get on a team and make sure your in-game progress counts.",
        "Events", 2, """
# Joining & playing an event

Any player with a claimed OSRS account can take part in their clan's events.
Here's how to get in and make sure your effort counts.

## Before you join

1. **[Sign in with Discord](/api/auth/login).** You'll need at least one
   [claimed OSRS account](/docs/link-account) — that's how an event knows which
   in-game character is you.
2. **Turn on API connections in the plugin.** In the
   [RuneLite plugin](/docs/runelite-plugin) settings, enable **Use API
   Connections**. This is what lets your drops and achievements reach
   DropTracker and count toward tasks.

> **Without "Use API Connections", your progress will not count.** If your
> completions aren't registering, check this setting first.

## Joining

Open the [events list](/events) or find the event on your group's page, then
press **Join**. What happens next depends on how the organiser set the event up:

- **Pick your team** — you choose which team to join. Some events require a
  **join code** from your clan first.
- **Auto-assigned** — you're placed on the smallest team automatically to keep
  things balanced.
- **Sign-up pool** — you sign up now, and an admin sorts everyone into teams
  before the event starts.
- **Admin-assigned** — an admin adds you to a team; there's no self sign-up.

Your clan may also post a **Sign up** button in Discord — clicking it adds you to
the pool without leaving the server.

## While the event runs

The event page updates in real time. You'll see the board or task list with your
team's progress, the team standings, and a live feed of completions as they land.

Most tasks complete on their own the moment a qualifying drop, kill, personal
best, or level-up is tracked for you. Tasks the organiser marked **needs review**
wait for an admin to confirm them, and a few types (like EHP/EHB or custom goals)
are awarded by an admin by hand.

## What counts toward a task

Different task types are credited from different things you do in-game:

| Task | Completed by |
|---|---|
| Item collection | Drops and collection-log entries for the listed item(s) |
| Kill count | Tracked kills of the target boss/NPC |
| Personal best | A tracked personal best that beats the time limit |
| XP / Skill level | XP gained, or a level reached, during the event |
| Loot value | GP from drops (optionally only from certain NPCs) |
| EHP / EHB / Custom | Awarded manually by an admin |

Only progress made **after you join a team** counts. If an admin moves you to a
different team mid-event, your credit starts fresh on the new team — so choose
carefully.

## Troubleshooting

- **Nothing is counting.** Confirm **Use API Connections** is on, and that the
  relevant category (drops, collection log, etc.) is enabled in the plugin.
- **A completion is stuck "pending".** That task needs admin review — it'll count
  once an organiser confirms it.
- **You joined the wrong team.** Ask an admin to move you (this resets your credit
  on the new team).
""",
    ),

    "events-create": (
        "Creating & running an event",
        "The create form, event settings, and the event lifecycle.",
        "Events", 3, """
# Creating & running an event

Events are run from your group's **Events** tab (**Manage group → Events**).
Creating one requires the **Patron** tier ([see premium](/docs/premium)), and a
group can have a limited number of events running at once.

## Create the event

Choose **Brand-new event** (or **Start from a template** to reuse a saved one —
see [Reusing your work](/docs/events-templates)), then fill in the basics:

- **Name & description** — what players see on the event page.
- **Start and end dates** — optional. A scheduled event **auto-activates** at its
  start time (once it passes the readiness checks below) and can auto-end at its
  end time. Leave them blank to start and stop it by hand.
- **Team formation** — how players end up on teams. See
  [Teams, sign-ups & clan vs clan](/docs/events-teams) for the full breakdown.
- **Join code** — an optional code players must enter to self-join, so only your
  clan can get in.
- **Submission policy** — which submissions count. See
  [Reviewing, scoring & Discord](/docs/events-review).

Press **Create event**. A new event starts as a **draft** so you can build it in
private before anyone can join.

## Build it out

While it's a draft, add the pieces from the same Events screen:

- **[Tasks](/docs/events-tasks)** — the objectives players complete.
- **[A bingo board](/docs/events-bingo)** — optional; arrange tasks on a grid.
- **[Teams](/docs/events-teams)** — who competes against whom.
- **[Discord channels](/docs/events-review)** — where announcements go.

You can change the event's name, description, dates, formation mode, join code
and policies at any time from **Edit** on the same screen.

## The lifecycle

Events move through three stages:

| Stage | What it means |
|---|---|
| **Draft** | You're building it. Players can't join yet; nothing is scored. |
| **Active** | It's live. Players join, submissions count, standings update. |
| **Past** | It's over. Final standings are posted and history is read-only. |

### Activating

Press **Activate** to go live (or let a scheduled start do it). Before an event
can activate it must pass a few checks:

- at least **one team**,
- a **complete bingo board** if the event uses one (every cell filled), and
- an **end date in the future**, if one is set.

Because the Patron tier allows only a limited number of **simultaneously active
events**, activation is blocked if you're already at that limit — end an existing
event first.

### Ending

Press **End event** (or let the scheduled end time do it). Ending locks the
event, freezes standings, and posts the **final results** to your event's Discord
channel. Ending can't be undone.

## Reusing an event

Once you've built a great event, you don't have to start over next time. **Save
it as a template** and spin up a fresh copy whenever you like — see
[Reusing your work](/docs/events-templates).
""",
    ),

    "events-tasks": (
        "Building tasks",
        "Task types, the drag-and-drop item/NPC builder, points, and review.",
        "Events", 4, """
# Building tasks

Tasks are the heart of an event — each one is a single objective worth some
**points**. Add them from the **Tasks** section of your event: **New task** to
build one, or **From library** to copy an existing one.

## Task types

Pick the type that matches your objective; the form then asks only for the fields
that type needs.

| Type | Goal | How it completes |
|---|---|---|
| **Item collection** | Get a specific item, or items from a list | Drops & collection-log entries |
| **Kill count** | Kill an NPC a number of times | Tracked kills |
| **Personal best** | Beat a boss within a time limit | Tracked personal bests |
| **XP target** | Gain XP in a skill | XP during the event |
| **Skill level** | Reach a level in a skill | Level reached during the event |
| **Loot value** | Earn a GP amount | Drops (optionally from set NPCs) |
| **EHP / EHB** | Reach an efficiency goal | Awarded manually by an admin |
| **Custom** | Anything else | Awarded manually by an admin |

> Item and NPC names must be **exact in-game names** — the tracker matches by
> name. The search picker only offers real names, so you never have to guess the
> spelling.

## The item & NPC picker

Item- and NPC-based tasks use a **drag-and-drop picker**. Type in the search box
on the left and matching results appear instantly with their icons. Add one by
**clicking it** or **dragging it** into the selection panel on the right; remove
it by dragging it back out or clicking the **×**. Everything drag does, click
does too — handy on touchscreens.

### Collection modes

Item-collection tasks have four modes:

- **Single item** — one item, with a quantity (e.g. 5× Dragon warhammer).
- **Any item from a list** — getting *any one* of the listed items completes it.
- **All items from a list** — the team must collect *every* item on the list.
- **Points from a list** — each item is worth points and the team races to a
  points goal. Give each item its own weight so rare drops are worth more.

## Points & review

- **Points** — what the task adds to a team's score when completed. A team's total
  is the sum of everything it has finished, plus any board bonuses.
- **Completions require admin review** — tick this for tasks that can't be trusted
  to auto-verify. Completions then wait in the
  [review queue](/docs/events-review) until an admin confirms them. EHP, EHB and
  Custom tasks are always awarded by hand.

## Editing & deleting tasks

Everything about a task is editable after you create it — **including item lists
and source NPCs**. Use **Edit** to change the goal, points, review flag or list
contents in place; use **Remove** to delete it.

Removing a task erases its progress and completions. If the task sits on a bingo
board, its **cell stays on the board, unbound**, ready for you to drop a new task
into it from the [board designer](/docs/events-bingo).

## Sharing tasks: the task library

Every task you create is also saved to the reusable **task library**, so you — and
optionally other clans — can drop it into future events without rebuilding it.
Each task carries a visibility:

- **Public** — any clan can find and reuse it from their library.
- **Private** — saved for your clan's future events only.

To reuse one, press **From library** when adding a task and search the presets:
curated tasks, anything shared publicly, and your clan's private saves. See
[Reusing your work](/docs/events-templates) for more.
""",
    ),

    "events-bingo": (
        "Bingo boards",
        "The board designer, line and blackout bonuses, and free cells.",
        "Events", 5, """
# Bingo boards

A bingo board lays your tasks out on a grid. Players complete cells, and you can
award **bonus points** for finishing full lines or the whole board — the classic
clan-bingo format, scored automatically.

## Designing the board

From your draft event, open the **Board** designer and choose a size (for example
5×5). Each cell can hold:

- **an existing task** from your event,
- **a task from the library** (copied into the event when you place it),
- **a brand-new task** you create inline, or
- **nothing** — a **free cell** that every team starts with already completed.

Arrange tasks across the grid however you like while the event is a draft.

> The board is **locked once the event starts** — finalise your layout before you
> activate. (You can still tweak individual tasks' points and review flags.)

## Bonuses

In the event's settings you can set two optional bonuses:

- **Line points** — awarded each time a team completes a full row, column, or
  diagonal.
- **Blackout points** — awarded when a team completes the *entire* board.

Bonuses are scored automatically as cells are completed, and they're taken back
correctly if a completion is later revoked.

## Free cells

Any empty cell is a **free space**: every team has it completed from the moment
the event goes live, exactly like the middle square on a traditional bingo card.
Use them to make lines more achievable or to give everyone a head start.

## Tips

- Leaving the centre cell empty gives the familiar free-space feel.
- Deleting a task that's on the board leaves its cell **unbound** rather than
  removing the cell, so the grid keeps its shape — just bind a new task to it.
- Balance line and blackout bonuses against per-task points so the board rewards
  both steady progress and the final push.
""",
    ),

    "events-board": (
        "Board game events",
        "How the dice-board event type works: turns, coins, the power-up shop, and every item.",
        "Events", 6, """
# Board game events

A **board game** event turns your task list into a race across a tile board. Every
team has a game piece; completing the task on your current tile earns **coins** and
lets you **roll the dice** to move forward. First team to reach the **finish tile**
wins. Along the way you spend coins in a **power-up shop** to speed yourself up — or
to sabotage your rivals.

> Board game is a newer event type. If you don't see it when creating an event, ask
> a DropTracker admin to enable it for your group.

## How a turn works

Play is **not** strictly turn-based — every team plays at the same time, at their
own pace. Your team's loop is:

1. **Land on a tile.** Each tile has a difficulty (Air ▸ Water ▸ Earth ▸ Fire, easy
   → elite). Landing rolls a random task of that difficulty from the event's task
   pool, so different teams get different tasks.
2. **Complete the task.** Progress is tracked automatically from your drops / KC /
   collection log, exactly like any other event task.
3. **Earn coins.** Harder tiles pay more (Air 10 → Fire 50 by default).
4. **Roll the dice** and move that many tiles forward.
5. Repeat until someone reaches the finish.

**Auto vs. manual roll.** Your group leader chooses whether the dice roll
automatically the moment you finish a task, or whether a team member has to press
**Roll** each turn. Either way, if you ever get stuck the game keeps itself moving —
rest tiles and stalls are handled for you.

**Stuck on an impossible task?** The optional **mercy rule** auto-completes a task
that's been open too long (for no coins) so bad luck can't freeze your team.

## Coins & the shop

Coins are a per-event currency — they are **not** real GP and don't affect the
prize pot. You earn them by completing tasks and spend them in the **shop** on
power-ups. Buy an item and it goes into your **bag**; use it whenever you like.

- **Cooldowns.** Each item has a *type* (movement, offensive, defensive, economy,
  utility). Using one item of a type briefly locks the other items of that type —
  the shop shows when each becomes usable again.
- **Restocking.** Some events limit an item's stock. If your leader set a restock
  cadence, the shop refills bought-out items every so many turns/hours/days (at a
  fixed or random time).
- **Refunds.** If a leader disables an item after you bought it, its coins are
  refunded automatically the next time you try to use it.

## Power-up glossary

**Move faster**
- **Teleport tablet** — jump forward without doing a task.
- **Extra dice / Chosen roll** — add a die to your next roll, or pick its value.
- **Reroll move** — undo your last move and roll again.

**Change your task**
- **Reroll scroll** — swap your current task for another of the same tier.
- **Escape crystal** — reroll drawing from one tier *easier*.
- **Choose task** — draw a few candidates and pick one.
- **Skip token** — finish the current task instantly (no coins).

**Economy**
- **Coin chest** — triple the coins from your next completed task.
- **Gloves of Silence** — your next roll tolls coins from every rival you pass.

**Attack a rival** (pick a target team)
- **Ice barrage** — freeze a team: their next rolls move 0 tiles.
- **Bandos godsword** — knock a team back several tiles.
- **Mischievous Rat** — steal a random item from a team's bag.
- **Intricate pouch** — reroll a rival's current task.

**Defend yourself**
- **Spirit shield** — absorbs the next attack against you.
- **Ward scroll / Rat poison** — absorbs specific kinds of attack.
- **Prayer potion (cleanse)** — clears a freeze and other debuffs off your team.
- **Dinh's Bulwark** — drop a roadblock on a tile; the next team to cross it is
  stopped (and may lose a turn).

When you're attacked — frozen, knocked back, robbed — you'll see it on the board and
in your team's Discord channel, and a badge shows any effect currently on you
(❄️ frozen, 🛡 shielded, ✨ boosted, and so on).

## For group leaders

Everything is configurable per event from the **Board** and **Board settings**
sections of your draft:

- **Movement** — dice count/sides or a fixed step; auto or manual roll; who may
  roll.
- **Board** — upload art or **generate a board** in one click; set each tile's
  difficulty (or pin a specific task); choose how tiles render (rune icon / outline
  / invisible).
- **Economy** — coins on/off, per-difficulty rewards, starting coins.
- **Shop** — turn the shop on/off, enable/disable individual items, set prices,
  stock and per-team caps, and the restock cadence.
- **Mercy & win** — the anti-stall timer, and the win rule (first to the finish,
  ties broken by task score).

Set a board's difficulties so every tier you use has tasks in the pool, add at least
a start and a finish tile, and give each team a piece — then activate. The layout
locks at activation; settings stay editable.
""",
    ),

    "events-teams": (
        "Teams, sign-ups & clan vs clan",
        "How players end up on teams, the sign-up pool, and clan-vs-clan events.",
        "Events", 6, """
# Teams, sign-ups & clan vs clan

Every event is scored by **team** — even a solo event is just teams of one. How
players end up on a team is set by the event's **formation mode**, and
DropTracker supports everything from open sign-up to challenging a rival clan.

## Formation modes

Choose one when you create the event (changeable while it's a draft):

| Mode | How players join |
|---|---|
| **Self sign-up — pick your team** | Players choose their own team (optionally gated by a join code). |
| **Self sign-up — auto-assigned** | Players sign up and are placed on the smallest team automatically. |
| **Sign-up pool — sort later** | Players sign up into a pool with no team; you sort them before it starts. |
| **Admin assign** | No self sign-up; admins place every player. |

## Managing teams

From the **Teams** section you can:

- **Create teams** and name them.
- **Add or remove players** — search your group's members and add them to a team,
  or remove them.
- **Rename** a team to fix a typo, or **delete** one made by mistake (its roster
  and progress are cleared and standings recompute).

> Moving a player to a different team **resets their credit** on the new team —
> their join time, which is what progress is measured from, starts over.

## The sign-up pool

In **sign-up pool** mode, players opt in without a team and you sort them when
you're ready, from the **Sign-ups** section:

- **Assign** players onto teams by hand, or
- **Randomize** the whole pool across your teams — re-roll as often as you like
  until it looks fair.
- **Post a "Sign up" button to Discord** so members can join the pool straight
  from your server.

## Clan vs clan

Want to compete against another clan? Create the event in **Clan vs clan** mode
and **invite an opponent clan**. Once they accept:

- Each team belongs to one of the participating clans.
- Both clans' admins can **co-manage** the event — the opponent doesn't need their
  own Patron subscription to take part; only the host pays.
- Players are added from their own clan's roster.

Everything else — tasks, board, scoring — works exactly as in a standard event.
""",
    ),

    "events-review": (
        "Reviewing, scoring & Discord",
        "The review queue, manual awards, submission policies, and Discord routing.",
        "Events", 7, """
# Reviewing, scoring & Discord

Most completions land automatically, but you stay in control: a review queue for
anything that needs a human, manual awards for the things that can't be tracked,
and Discord channels to keep your clan in the loop.

## How scoring works

A team's score is the total **points** of every task it has completed, plus any
**bingo line/blackout bonuses**. Scores update live as completions are applied,
and the event's admin view shows the standings and a per-task progress matrix.

## The review queue

Some completions don't apply straight away — they wait in **Review** for an admin.
A completion is held for review when:

- the **task** is marked *completions require admin review*, or
- the **event** requires confirmation for *every* completion, or
- the event's **submission policy** holds non-plugin submissions (below).

For each pending item you can **Confirm** it — it then applies exactly like an
automatic completion, points and board cells and all — or **Reject** it (no
points, optionally with a note).

## Submission policies

An event's submission policy decides which submissions are trusted:

| Policy | Effect |
|---|---|
| **All submissions count** | Everything counts immediately, however it was sent. |
| **Non-plugin submissions need review** | Plugin submissions count at once; anything else queues for review. |
| **Plugin submissions only** | Only RuneLite-plugin submissions count; others are ignored. |

This is why players are asked to enable **Use API Connections** — plugin
submissions are the trusted path.

## Manual awards & revokes

For tasks that can't be auto-tracked (EHP, EHB, Custom) — or to credit something
earned before a player joined — use **Award** to grant a completion to a team by
hand. You can award a set quantity or mark the task fully complete. If something
was credited in error, **Revoke** it and the event re-computes that team's
progress, score, and any affected board bonuses.

## Discord destinations

From the event's **Discord** settings, point the event at a server and choose a
channel for each kind of message:

- **Announcements** — the event going live, ending, and other milestones.
- **Completions** — individual task completions as they happen.
- **Leaderboard** — standings updates.
- **Admin** — organiser-facing notices.

You can also have DropTracker create a **Discord scheduled event** for the start,
and **ping specific roles** for key moments. Any server the bot is in can be
targeted, so a dedicated events server works fine.
""",
    ),

    "events-templates": (
        "Reusing your work",
        "The shared task library and saving an event as a reusable template.",
        "Events", 8, """
# Reusing your work

Building a good event takes effort — the task library and event templates let you
keep that effort and reuse it.

## The task library

Every task you create in an event is saved to the **task library** as a reusable
preset. When adding a task, press **From library** to search:

- **Curated presets** maintained by the DropTracker team,
- tasks other clans have **shared publicly**, and
- your own clan's **private saves**.

Picking one copies it into your event as an ordinary task you can then edit or
delete — the original preset is untouched.

Each task you make carries a **visibility**, set from the **Task library**
dropdown in the task form:

- **Public** — any clan can find and reuse it.
- **Private** — kept for your clan's future events only.

Re-saving a task with the same name updates its preset rather than creating a
duplicate.

## Event templates

A template is a snapshot of a whole event's **structure** — its settings, tasks,
bingo layout, and team names — that you can turn into a fresh event whenever you
want.

### Saving a template

From an event (in any state), use **Save as template**, give it a name, and choose
**Public** (any clan can start from it) or **Private** (your clan only). Re-saving
with the same name updates the existing template.

A template captures structure **only**. It never carries dates, players, scores,
join codes, Discord settings, or clan-vs-clan opponents — those are unique to each
run.

### Starting from a template

When creating an event, choose **Start from a template**, pick one, and press
**Create event from template**. You get a fresh **draft** pre-filled with all its
tasks and board, ready for you to set new dates and activate. A clan-vs-clan
template re-runs as a standard event (invite your opponent again for the new run).

If an item or NPC in a saved task no longer exists (renamed in-game, say), that
one task is **skipped** and reported rather than failing the whole thing — its
board cell comes through unbound so you can rebind it in the designer.
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
    ap.add_argument(
        "--prefix",
        help="Only apply pages whose slug starts with this (e.g. 'events'). "
        "Use this to re-drop one section without clobbering CMS edits to the "
        "rest — a full run overwrites every listed slug.",
    )
    args = ap.parse_args()

    pages = PAGES
    if args.prefix:
        pages = {s: v for s, v in PAGES.items() if s.startswith(args.prefix)}
        if not pages:
            print(f"No pages match prefix {args.prefix!r}.")
            return 1

    created = updated = 0
    for slug, (title, description, category, order, body) in pages.items():
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
    print(f"\n{'Previewed' if args.dry_run else 'Done'}: {created} created, {updated} updated, {len(pages)} total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
