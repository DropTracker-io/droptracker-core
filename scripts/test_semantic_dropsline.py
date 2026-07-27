import argparse
import asyncio
from collections import Counter

import aiohttp

from osrs_api.semantic import SemanticAPI


class _MinimalWikiClient:
    """
    Minimal client shim for SemanticAPI so it can be used standalone in scripts.
    """

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def get_wiki_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


async def _run(item_name: str, npc_name: str, limit: int) -> int:
    client = _MinimalWikiClient()
    api = SemanticAPI(client)
    try:
        # Ask the API which pages claim to drop the item.
        query = (
            "bucket('dropsline')"
            ".select('page_name')"
            f".where('item_name', {api._bucket_quote(item_name)})"
            ".run()"
        )
        result = await api._bucket_query(query)
        bucket_data = result.get("bucket", []) or []

        page_names = []
        for row in bucket_data:
            page = (row.get("page_name") or "").strip()
            if not page:
                continue
            if "#" in page:
                page = page.split("#", 1)[0]
            page_names.append(page)

        counts = Counter(page_names)

        print(f"Item: {item_name}")
        print(f"NPC : {npc_name}")
        print(f"rows: {len(bucket_data)} | unique pages: {len(counts)}")
        print("")

        for page, cnt in counts.most_common(limit):
            print(f"{cnt:>4}  {page}")

        print("")
        ok = await api.check_drop(item_name=item_name, npc_name=npc_name)
        print(f"check_drop(...) -> {ok}")

        if not ok:
            # Helpful hint for the common Gauntlet confusion.
            if "Gauntlet" in npc_name:
                print(
                    "NOTE: Many Gauntlet uniques are attributed to "
                    "'Reward Chest (The Gauntlet)' in dropsline."
                )

        return 0 if ok else 2
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debug OSRS Wiki Bucket dropsline page_name sources for an item."
    )
    parser.add_argument("--item", required=True, help="Item name, exact wiki item_name")
    parser.add_argument("--npc", required=True, help="NPC/encounter name used by DropTracker")
    parser.add_argument(
        "--limit", type=int, default=30, help="Max unique page_name values to print"
    )
    args = parser.parse_args()

    raise SystemExit(asyncio.run(_run(args.item, args.npc, args.limit)))


if __name__ == "__main__":
    main()

