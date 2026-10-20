"""Seed the "Removing your data" docs page (web137a).

The docs CMS (``/admin/docs``) is the source of truth, so this only creates
the page when its slug does not exist; it never overwrites CMS edits. Run it
once when the data-removal feature is deployed.

Run:
    venv/bin/python -m scripts.seed_data_removal_docs            # dry run
    venv/bin/python -m scripts.seed_data_removal_docs --apply    # create the page
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLUG = "data-removal"
TITLE = "Removing your data"
DESCRIPTION = ("Remove an account's data, or a whole group, from The DropTracker: "
               "how to ask, how we verify it, and what is kept.")
CATEGORY = "Account"
ORDER = 3

BODY = """\
You can ask us to remove everything The DropTracker holds about one of your
accounts, or, if you own a group, the whole group. You don't need to explain
why.

## Remove an account's data

1. Sign in and open [Settings](/settings).
2. Under **Your data**, find the account and press **Remove this account's
   data**.
3. Type the account name to confirm. This opens a support ticket.
4. In the ticket you'll see a short phrase, like `DropTracker removal 482913`.
   Log in to that account, type the phrase in the chatbox, and post a
   screenshot of your **whole RuneLite client** in the ticket.
5. Our staff check the screenshot and approve the request.

Only accounts you have [claimed](/docs/link-account) show up here. If
your account was never claimed, or someone else claimed it, open a support
ticket and we'll sort it out with you.

### Why we check

Anyone can claim a name that nobody has claimed yet, so a claim alone doesn't
prove an account is yours. The phrase in your chatbox does: only the person
logged in to the account can type it. This stops someone from wiping another
player's history.

## Remove a group

The group's **owner** can ask for the whole group to be removed:

1. Open the group's **Roles & access** page.
2. Press **Request group deletion** and type the group name to confirm.
3. A support ticket opens. Every admin of the group who is in our Discord is
   added to it and asked to confirm or object.
4. Once the group's leaders agree, our staff approve the request.

Removing a group deletes its settings, boards, events, points and history.
Members' own drops and accounts are **not** removed; they belong to the
players.

## What happens after approval

- **Straight away:** a removed account is hidden from every leaderboard, board
  and search.
- **For 7 days:** nothing is deleted yet. You can cancel from Settings (or the
  group's Roles & access page), or ask in your ticket.
- **After 7 days:** the data is erased: drops, personal bests, collection log,
  combat achievements, pets, deaths, points, badges and screenshots.
- **For 7 more days:** we keep a private backup in case the removal was a
  mistake. Then it is deleted for good.

Some things are handled differently so other people aren't affected:

- **Discord messages** our bot posted about the account are not deleted, so
  your clan's channel history still reads in order. Instead they are edited
  to say *Deleted user*, and their screenshots are removed.
- **Past events** keep their scores, so other teams' results don't change.
  Your part of the score is moved to an anonymous "Removed player" with no
  link to you.
- **Your support ticket** is kept as the record of your request.

Removing one account doesn't touch your other accounts or your DropTracker
sign-in.

## Stopping it coming back

Removal is a one-off. Your data will start collecting again if:

- the DropTracker plugin is still installed and running on that account, or
- a clan that uses The DropTracker still lists the account in its
  Wise Old Man group.

To stay removed, uninstall the plugin (or turn it off in RuneLite's plugin
list) before you ask.

See also our [Privacy Policy](/privacy).
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from db.models import DocsPage, session

    try:
        existing = session.query(DocsPage).filter(DocsPage.slug == SLUG).first()
        if existing is not None:
            print(f"/docs/{SLUG} already exists (id {existing.id}); leaving it alone")
            return 0
        print(f"would create /docs/{SLUG} '{TITLE}' in {CATEGORY} (order {ORDER}), "
              f"{len(BODY)} chars")
        if not args.apply:
            print("(dry run)")
            return 0
        session.add(DocsPage(slug=SLUG, title=TITLE, description=DESCRIPTION,
                             category=CATEGORY, order=ORDER, body_md=BODY))
        session.commit()
        print("created")
        return 0
    finally:
        session.remove()


if __name__ == "__main__":
    sys.exit(main())
