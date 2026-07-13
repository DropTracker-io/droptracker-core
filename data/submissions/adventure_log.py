"""Adventure Log submissions processor."""

import re
from datetime import datetime

from .common import (
    select_session_and_flag,
    ensure_player_by_name_then_auth,
    convert_to_ms,
    get_true_boss_name,
    debug_print,
)


PB_LINE_PATTERN = re.compile(
    r"`(?P<boss>.+?)`\s*-\s*`(?P<team>.+?)`\s*:\s*`(?P<time>.+?)`"
)


def _collect_adventure_log_lines(payload: dict) -> list[str]:
    """Collect PB lines from either `adventure_log` or numeric page keys."""
    lines: list[str] = []
    packed = payload.get("adventure_log")
    if packed:
        try:
            lines.extend([ln.strip() for ln in str(packed).split("\n") if ln.strip()])
        except Exception:
            pass
    else:
        numeric_keys = []
        for key in payload.keys():
            if str(key).isdigit():
                numeric_keys.append(int(str(key)))
        for idx in sorted(numeric_keys):
            page = payload.get(str(idx))
            if not page:
                continue
            try:
                lines.extend([ln.strip() for ln in str(page).split("\n") if ln.strip()])
            except Exception:
                continue
    return lines


def _parse_pet_ids(payload: dict) -> list[int]:
    """Parse pet ids from `pet_list` or `Pets` payload values."""
    raw = payload.get("pet_list", payload.get("Pets"))
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        result = []
        for val in raw:
            try:
                result.append(int(val))
            except Exception:
                continue
        return result
    raw_str = str(raw)
    return [int(match) for match in re.findall(r"\d+", raw_str)]


async def adventure_log_processor(adventure_log_data, external_session=None):
    debug_print("adventure_log_processor")
    print("Got adventure log data:", adventure_log_data)
    session, _ = select_session_and_flag(external_session)

    player_name = adventure_log_data.get("player_name", adventure_log_data.get("player", None))
    account_hash = adventure_log_data.get("acc_hash", adventure_log_data.get("account_hash", None))
    player, authed, user_exists = await ensure_player_by_name_then_auth(
        session, player_name, account_hash, ""
    )
    if not player:
        return
    player_id = player.player_id
    if not user_exists or not authed:
        return

    from db import PersonalBestEntry, ItemList, PlayerPet

    pb_lines = _collect_adventure_log_lines(adventure_log_data)
    pet_ids = _parse_pet_ids(adventure_log_data)
    debug_print(
        f"Adventure log parse start for {player_name}: "
        f"pb_line_count={len(pb_lines)}, pet_id_count={len(pet_ids)}"
    )
    pb_updates = 0
    pb_inserts = 0
    pets_added = 0

    try:
        # Upsert PBs from adventure log pages.
        for raw_line in pb_lines:
            match = PB_LINE_PATTERN.search(raw_line)
            if not match:
                debug_print(f"Adventure log PB skipped (parse mismatch): line='{raw_line}'")
                continue

            boss_name = str(match.group("boss") or "").strip()
            # Raw log fragments arrive as "(2", "(2 players)", "5 s", … —
            # normalize to the canonical team encodings so one team size
            # never splits into parallel PB boards (suggestion #50).
            from utils.npc_names import sanitize_team_size

            team_size = sanitize_team_size(match.group("team"))
            kill_time = str(match.group("time") or "").strip()
            if not boss_name or not team_size or not kill_time:
                debug_print(
                    "Adventure log PB skipped (missing parsed values): "
                    f"boss='{boss_name}', team='{team_size}', time='{kill_time}'"
                )
                continue

            _, npc_id = get_true_boss_name(boss_name)
            if npc_id is None:
                debug_print(
                    f"Adventure log PB skipped (unresolved npc_id): "
                    f"boss='{boss_name}', team='{team_size}', time='{kill_time}'"
                )
                continue

            try:
                time_ms = int(convert_to_ms(kill_time))
            except Exception:
                debug_print(
                    f"Adventure log PB skipped (time conversion error): "
                    f"boss='{boss_name}', team='{team_size}', time='{kill_time}'"
                )
                continue
            if time_ms <= 0:
                debug_print(
                    f"Adventure log PB skipped (non-positive time_ms): "
                    f"boss='{boss_name}', team='{team_size}', time_ms={time_ms}"
                )
                continue

            existing_pb = (
                session.query(PersonalBestEntry)
                .filter(
                    PersonalBestEntry.player_id == player_id,
                    PersonalBestEntry.npc_id == npc_id,
                    PersonalBestEntry.team_size == team_size,
                )
                .first()
            )

            if existing_pb:
                # Adventure logs represent known bests, so keep the minimum time.
                if existing_pb.personal_best is None or time_ms < int(existing_pb.personal_best):
                    old_pb = existing_pb.personal_best
                    existing_pb.personal_best = time_ms
                    existing_pb.kill_time = time_ms
                    existing_pb.new_pb = True
                    existing_pb.date_added = datetime.now()
                    pb_updates += 1
                    debug_print(
                        f"Adventure log PB updated: player_id={player_id}, npc_id={npc_id}, "
                        f"team='{team_size}', old_pb={old_pb}, new_pb={time_ms}"
                    )
                else:
                    debug_print(
                        f"Adventure log PB unchanged (existing is better/equal): "
                        f"player_id={player_id}, npc_id={npc_id}, team='{team_size}', "
                        f"existing_pb={existing_pb.personal_best}, incoming={time_ms}"
                    )
            else:
                session.add(
                    PersonalBestEntry(
                        player_id=player_id,
                        npc_id=npc_id,
                        team_size=team_size,
                        personal_best=time_ms,
                        kill_time=time_ms,
                        new_pb=True,
                        used_api=True,
                        unique_id=adventure_log_data.get("guid"),
                    )
                )
                pb_inserts += 1
                debug_print(
                    f"Adventure log PB inserted: player_id={player_id}, npc_id={npc_id}, "
                    f"team='{team_size}', pb={time_ms}"
                )

        # Insert pets from adventure log if not already stored for player.
        for pet_id in pet_ids:
            item_object = session.query(ItemList).filter(ItemList.item_id == int(pet_id)).first()
            if not item_object:
                debug_print(f"Adventure log pet skipped (unknown item_id): {pet_id}")
                continue
            existing_player_pet = (
                session.query(PlayerPet)
                .filter(
                    PlayerPet.player_id == player_id,
                    PlayerPet.item_id == item_object.item_id,
                )
                .first()
            )
            if existing_player_pet:
                debug_print(
                    f"Adventure log pet already exists: player_id={player_id}, item_id={item_object.item_id}"
                )
                continue
            session.add(
                PlayerPet(
                    player_id=player_id,
                    item_id=item_object.item_id,
                    pet_name=item_object.item_name,
                    unique_id=adventure_log_data.get("guid"),
                )
            )
            pets_added += 1
            debug_print(
                f"Adventure log pet inserted: player_id={player_id}, "
                f"item_id={item_object.item_id}, pet_name='{item_object.item_name}'"
            )

        session.commit()
        debug_print(
            f"Adventure log processed for {player_name}: "
            f"pb_updates={pb_updates}, pb_inserts={pb_inserts}, pets_added={pets_added}"
        )
    except Exception as e:
        session.rollback()
        print(f"Error processing adventure log for {player_name}: {e}")

