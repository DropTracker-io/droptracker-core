#!/usr/bin/env python3
"""
Interactive helper to submit a drop to the Quart /webhook endpoint.

Capabilities:
- Prompts for player name; fetches the player's account hash from DB if available.
- Prompts for item name; resolves to item_id and uses the most recent DB drop value automatically.
- Attempts to resolve NPC (from most recent drop). Falls back to user prompt if needed.
- Optionally accepts an image URL; downloads and attaches it as multipart for server-side handling.
"""

import json
import uuid
import base64
import sys
import os
from typing import Optional, Tuple, List

from dotenv import load_dotenv

try:
    import requests  # pip install requests
except ImportError:
    print("This script requires the 'requests' package. Install it with: pip install requests")
    sys.exit(1)

# DB imports
try:
    from db import Session, ItemList, NpcList, Drop, Player
except Exception as e:
    print(f"Failed to import DB models. Ensure you're running from the project root. Error: {e}")
    sys.exit(1)

load_dotenv()

# ====== Configuration ======
API_BASE = os.getenv("DROP_CREATOR_API_BASE", "http://127.0.0.1:31323")
WEBHOOK_URL = f"{API_BASE}/webhook"

DEFAULT_QUANTITY = 1


def tiny_png_bytes() -> bytes:
    """Return bytes of a minimal 1x1 PNG to satisfy multipart form."""
    b64_png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X0n8AAAAASUVORK5CYII="
    )
    return base64.b64decode(b64_png)


def prompt(text: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{text}{suffix}: ").strip()
    return value or (default or "")


def choose_from_list(options: List[Tuple[int, str]], title: str) -> Optional[Tuple[int, str]]:
    """Prompt user to choose from a list of (id, name) tuples."""
    if not options:
        return None
    print(f"\n{title}")
    for idx, (oid, oname) in enumerate(options, start=1):
        print(f"{idx}) {oname} (id={oid})")
    while True:
        choice = input("Enter choice number (or press Enter to cancel): ").strip()
        if choice == "":
            return None
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(options):
                return options[index - 1]
        print("Invalid choice. Try again.")


def resolve_player_and_hash(session, player_name: str) -> Tuple[str, str]:
    """Return (normalized_player_name, account_hash). Prompts for hash if not in DB."""
    player = session.query(Player).filter(Player.player_name.ilike(player_name)).first()
    if player:
        acc_hash = player.account_hash or ""
        if not acc_hash:
            print("Player exists but has no account hash in DB.")
            acc_hash = prompt("Enter account hash for this player (required for auth)", "")
        return player.player_name, acc_hash
    print("Player not found in DB. The API may attempt to create it if you provide an account hash.")
    acc_hash = prompt("Enter account hash (leave blank to attempt without)", "")
    return player_name, acc_hash


def resolve_item_by_name(session, item_name: str) -> Optional[Tuple[int, str]]:
    """Resolve item by name. Try exact; if not found, allow fuzzy selection."""
    item = session.query(ItemList).filter(ItemList.item_name == item_name).first()
    if item:
        return item.item_id, item.item_name
    # Fuzzy search (basic contains), limit 10
    like_term = f"%{item_name}%"
    candidates = (
        session.query(ItemList.item_id, ItemList.item_name)
        .filter(ItemList.item_name.like(like_term))
        .limit(10)
        .all()
    )
    if not candidates:
        return None
    choice = choose_from_list(candidates, "Multiple items found. Choose one:")
    return choice if choice else None


def get_latest_db_value_and_npc(session, item_id: int) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Return (value, npc_id, npc_name) from the most recent drop for this item, if any."""
    last_drop = (
        session.query(Drop)
        .filter(Drop.item_id == item_id)
        .order_by(Drop.date_added.desc())
        .first()
    )
    if not last_drop:
        return None, None, None
    npc_name = None
    if last_drop.npc_id:
        npc = session.query(NpcList).filter(NpcList.npc_id == last_drop.npc_id).first()
        npc_name = npc.npc_name if npc else None
    return last_drop.value, last_drop.npc_id, npc_name


def build_payload_json(player_name: str,
                       account_hash: str,
                       item_id: int,
                       item_name: str,
                       npc_name: Optional[str],
                       value: int,
                       quantity: int,
                       guid: str,
                       extra_fields: Optional[List[dict]] = None) -> str:
    """Build the Discord-like payload_json expected by the endpoint."""
    embed_fields = [
        {"name": "type", "value": "drop"},
        {"name": "player_name", "value": player_name},
        {"name": "acc_hash", "value": account_hash},
        {"name": "auth_key", "value": ""},  # reserved
        {"name": "guid", "value": guid},
        {"name": "item_name", "value": str(item_name)},
        {"name": "item_id", "value": str(item_id)},
        {"name": "source", "value": str(npc_name or "")},
        {"name": "value", "value": str(value)},
        {"name": "quantity", "value": str(quantity)},
    ]
    if extra_fields:
        embed_fields.extend(extra_fields)
    payload = {
        "embeds": [
            {
                "title": "Drop Submission",
                "fields": embed_fields,
            }
        ]
    }
    return json.dumps(payload)


def parse_split_members_input(raw_input: str) -> List[str]:
    """Parse split-member input into a clean list of names.

    Supports:
    - comma-separated names
    - JSON array string: ["name1", "name2"]
    """
    if not raw_input:
        return []
    text = raw_input.strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                cleaned = []
                seen = set()
                for value in parsed:
                    name = str(value).strip() if value is not None else ""
                    if not name:
                        continue
                    key = name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned.append(name)
                return cleaned
        except Exception:
            pass

    cleaned = []
    seen = set()
    for part in text.replace("\n", ",").split(","):
        name = part.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


def download_image_from_url(url: str) -> Optional[Tuple[str, bytes, str]]:
    """Download image bytes from a URL for multipart upload. Returns (filename, bytes, content_type)."""
    try:
        resp = requests.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        # Guess filename from URL path
        filename = url.split("/")[-1] or "image.jpg"
        if "." not in filename:
            # Add extension from content type if missing
            ext = "jpg"
            if "png" in content_type.lower():
                ext = "png"
            elif "webp" in content_type.lower():
                ext = "webp"
            elif "gif" in content_type.lower():
                ext = "gif"
            filename = f"{filename}.{ext}"
        return filename, resp.content, content_type
    except Exception as e:
        print(f"Warning: Failed to download image from URL: {e}")
        return None


def main():
    session = Session()
    try:
        print(f"POST target: {WEBHOOK_URL}")
        # 1) Player
        input_player = prompt("Enter player name")
        player_name, account_hash = resolve_player_and_hash(session, input_player)
        if not player_name:
            print("Player name is required.")
            return
        # 2) Item
        input_item = prompt("Enter item name")
        resolved = resolve_item_by_name(session, input_item)
        if not resolved:
            print("Could not resolve item from DB. Please add the item first or try a different name.")
            return
        item_id, item_name = resolved
        # 3) Value and NPC from DB
        db_value, db_npc_id, db_npc_name = get_latest_db_value_and_npc(session, item_id)
        value = db_value if db_value is not None else 0
        npc_name = db_npc_name
        if npc_name is None:
            # Prompt for NPC name fallback
            maybe_npc = prompt("Enter NPC name (optional, leave blank to skip)", "")
            npc_name = maybe_npc or ""
        # 4) Quantity
        try:
            quantity = int(prompt("Enter quantity", str(DEFAULT_QUANTITY)))
        except Exception:
            quantity = DEFAULT_QUANTITY
        # 5) Optional image URL
        image_url = prompt("Enter image URL (optional)", "")
        extra_fields = []
        if image_url:
            # Add informational field; server will use uploaded file as source,
            # but we keep the original URL in the payload for reference.
            extra_fields.append({"name": "image_url", "value": image_url})
        # 6) Optional split members
        split_members_raw = prompt(
            "Enter split members (comma-separated or JSON array, optional)",
            "",
        )
        split_members = parse_split_members_input(split_members_raw)
        if split_members:
            # Send as JSON string so webhook parser can decode robustly.
            extra_fields.append(
                {"name": "players_included", "value": json.dumps(split_members)}
            )
            print(f"Split members included: {split_members}")
        else:
            print("No split members included.")
        guid = str(uuid.uuid4())
        payload_json = build_payload_json(
            player_name=player_name,
            account_hash=account_hash,
            item_id=item_id,
            item_name=item_name,
            npc_name=npc_name,
            value=value,
            quantity=quantity,
            guid=guid,
            extra_fields=extra_fields,
        )
        print(
            "Submission debug: "
            f"guid={guid}, player_name={player_name}, item_name={item_name}, "
            f"players_included={split_members if split_members else None}"
        )
        # Prepare multipart
        files = {}
        if image_url:
            downloaded = download_image_from_url(image_url)
            if downloaded:
                fname, fbytes, ctype = downloaded
                files["file"] = (fname, fbytes, ctype)
            else:
                # Fallback to tiny PNG
                files["file"] = ("image.png", tiny_png_bytes(), "image/png")
        else:
            # Always send a 'file' to ensure multipart/form-data handling server-side
            files["file"] = ("image.png", tiny_png_bytes(), "image/png")
        data = {"payload_json": payload_json}
        print("\nSubmitting drop...")
        resp = requests.post(WEBHOOK_URL, data=data, files=files, timeout=60)
        print(f"Status: {resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype.lower():
            try:
                print(json.dumps(resp.json(), indent=2))
            except Exception:
                print(resp.text)
        else:
            print(resp.text)
    finally:
        try:
            session.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()


