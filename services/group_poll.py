import json
import os
import time
import asyncio
import traceback
from pathlib import Path

import interactions
from interactions import (
    Extension, SlashContext, ComponentContext, Embed, ActionRow, Button,
    ButtonStyle, OptionType, Permissions, check, is_owner, listen,
    slash_command, slash_option
)
from interactions.api.events import Component

from db.models import session, GroupConfiguration

POLLS_DIR = Path("/store/droptracker/disc/data/polls")
POLLS_DIR.mkdir(parents=True, exist_ok=True)

RATE_LIMIT_DELAY = 1.5


def _next_poll_id() -> int:
    existing = [
        int(f.stem) for f in POLLS_DIR.glob("*.json") if f.stem.isdigit()
    ]
    return max(existing, default=0) + 1


def _poll_path(poll_id: int) -> Path:
    return POLLS_DIR / f"{poll_id}.json"


def _save_poll(data: dict) -> None:
    with open(_poll_path(data["poll_id"]), "w") as f:
        json.dump(data, f, indent=2)


def _load_poll(poll_id: int) -> dict | None:
    path = _poll_path(poll_id)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _load_all_polls() -> list[dict]:
    polls = []
    for path in sorted(POLLS_DIR.glob("*.json")):
        if path.stem.isdigit():
            with open(path) as f:
                polls.append(json.load(f))
    return polls


def _get_admin_user_ids() -> list[str]:
    """Collect every unique Discord user ID listed in any group's authed_users config."""
    admin_user_ids: set[str] = set()
    rows = (
        session.query(GroupConfiguration)
        .filter(GroupConfiguration.config_key == "authed_users")
        .all()
    )
    for row in rows:
        try:
            ids = json.loads(row.config_value)
            admin_user_ids.update(str(uid) for uid in ids)
        except (json.JSONDecodeError, TypeError):
            continue
    return list(admin_user_ids)


class GroupPollService(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        print("GroupPollService extension initialized.")

    # ── Owner-only: create & send a poll ─────────────────────────────

    @slash_command(
        name="send-poll",
        description="Create a poll and DM it to every group leader.",
        default_member_permissions=Permissions.ADMINISTRATOR,
        dm_permission=True,
    )
    @check(is_owner())
    @slash_option(
        name="title",
        description="Poll title / question",
        opt_type=OptionType.STRING,
        required=True,
    )
    @slash_option(
        name="description",
        description="Extra context shown to voters",
        opt_type=OptionType.STRING,
        required=True,
    )
    @slash_option(
        name="options",
        description='Comma-separated list of voting options (max 5). e.g. "Yes, No, Maybe"',
        opt_type=OptionType.STRING,
        required=True,
    )
    async def send_poll_cmd(
        self, ctx: SlashContext, title: str, description: str, options: str
    ):
        await ctx.defer(ephemeral=True)

        title = title.replace("\\n", "\n")
        description = description.replace("\\n", "\n")

        option_labels = [o.strip() for o in options.split(",") if o.strip()]
        if len(option_labels) < 2:
            return await ctx.send(
                "You must provide at least 2 comma-separated options.", ephemeral=True
            )
        if len(option_labels) > 5:
            return await ctx.send(
                "A maximum of 5 options is supported per poll.", ephemeral=True
            )

        poll_id = _next_poll_id()
        poll_data = {
            "poll_id": poll_id,
            "title": title,
            "description": description,
            "options": option_labels,
            "votes": {str(i): [] for i in range(len(option_labels))},
            "sent_to": [],
            "created_at": time.time(),
            "created_by": str(ctx.author.id),
        }
        _save_poll(poll_data)

        embed = Embed(
            title=f"Group Leader Poll #{poll_id}",
            description=f"### {title}\n\n{description}\n\n**Please vote below, according to your preference.**",
            color=0x5865F2,
        )
        for i, label in enumerate(option_labels):
            embed.add_field(name=f"Option {i + 1}", value=label, inline=True)
        embed.set_footer(text="Powered by the DropTracker | droptracker.io")

        buttons = [
            Button(
                style=ButtonStyle.PRIMARY,
                label=label,
                custom_id=f"poll_vote_{poll_id}_{i}",
            )
            for i, label in enumerate(option_labels)
        ]
        action_row = ActionRow(*buttons)

        admin_ids = _get_admin_user_ids()
        # admin_ids = [528746710042804247] ## Temporarily send to only me
        sent, failed = 0, 0

        for uid in admin_ids:
            try:
                user = await self.bot.fetch_user(int(uid))
                await user.send(content=f"Hey, {user.mention}!\nA new poll has been created for you to vote in:\n\n", embeds=[embed], components=[action_row])
                poll_data["sent_to"].append(uid)
                sent += 1
            except Exception:
                traceback.print_exc()
                failed += 1
            await asyncio.sleep(RATE_LIMIT_DELAY)

        _save_poll(poll_data)

        await ctx.send(
            f"Poll **#{poll_id}** sent to **{sent}** group leader(s)"
            + (f" ({failed} failed)" if failed else "")
            + ".",
            ephemeral=True,
        )

    # ── Owner-only: view poll results ────────────────────────────────

    @slash_command(
        name="poll-results",
        description="View the results of a group-leader poll.",
        default_member_permissions=Permissions.ADMINISTRATOR,
        dm_permission=True,
    )
    @check(is_owner())
    @slash_option(
        name="poll_id",
        description="The numeric poll ID (omit to list all polls)",
        opt_type=OptionType.INTEGER,
        required=False,
    )
    async def poll_results_cmd(self, ctx: SlashContext, poll_id: int = None):
        await ctx.defer(ephemeral=True)

        if poll_id is None:
            polls = _load_all_polls()
            if not polls:
                return await ctx.send("No polls have been created yet.", ephemeral=True)
            lines = []
            for p in polls:
                total_votes = sum(len(v) for v in p["votes"].values())
                lines.append(
                    f"**#{p['poll_id']}** — {p['title']}  "
                    f"({total_votes} vote{'s' if total_votes != 1 else ''}, "
                    f"sent to {len(p['sent_to'])})"
                )
            embed = Embed(
                title="All Polls",
                description="\n".join(lines),
                color=0x5865F2,
            )
            return await ctx.send(embeds=[embed], ephemeral=True)

        poll = _load_poll(poll_id)
        if not poll:
            return await ctx.send(
                f"No poll found with ID **#{poll_id}**.", ephemeral=True
            )

        embed = Embed(
            title=f"Results — Poll #{poll_id}",
            description=poll["description"],
            color=0x5865F2,
        )
        embed.add_field(name="Question", value=poll["title"], inline=False)

        total_votes = sum(len(v) for v in poll["votes"].values())
        for i, label in enumerate(poll["options"]):
            voter_ids = poll["votes"].get(str(i), [])
            count = len(voter_ids)
            pct = (count / total_votes * 100) if total_votes else 0
            bar_filled = round(pct / 10)
            bar = "█" * bar_filled + "░" * (10 - bar_filled)
            embed.add_field(
                name=f"{label}",
                value=f"`{bar}` **{count}** ({pct:.0f}%)",
                inline=False,
            )

        embed.set_footer(
            text=f"Total votes: {total_votes} | Sent to: {len(poll['sent_to'])} leaders"
        )
        await ctx.send(embeds=[embed], ephemeral=True)

    # ── Component listener: record votes ─────────────────────────────

    @listen(Component)
    async def on_poll_vote(self, event: Component):
        ctx: ComponentContext = event.ctx
        custom_id: str = ctx.custom_id

        if not custom_id.startswith("poll_vote_"):
            return

        parts = custom_id.split("_")
        if len(parts) != 4:
            return

        try:
            poll_id = int(parts[2])
            option_idx = str(parts[3])
        except (ValueError, IndexError):
            return

        poll = _load_poll(poll_id)
        if not poll:
            return await ctx.send(
                "This poll no longer exists.", ephemeral=True
            )
        if option_idx not in poll["votes"]:
            return await ctx.send(
                "Invalid option for this poll.", ephemeral=True
            )

        voter = str(ctx.author.id)

        for idx, voters in poll["votes"].items():
            if voter in voters:
                voters.remove(voter)

        poll["votes"][option_idx].append(voter)
        _save_poll(poll)

        chosen_label = poll["options"][int(option_idx)]
        await ctx.send(
            f"Your vote for **{chosen_label}** on poll **#{poll_id}** has been recorded. Thank you!",
            ephemeral=True,
        )
