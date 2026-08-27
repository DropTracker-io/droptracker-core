/**
 * Extract the collection log's structure — tabs, pages and the item id of every
 * slot — from the OSRS game cache.
 *
 * Why the cache and not the wiki: the wiki publishes item *names*, and a name
 * does not identify an item. Half the awkward slots are one of several versions
 * of the same thing (the log holds the *uncharged* Alchemist's amulet, the
 * *empty* Master scroll book), so resolving names to ids is a guess that has to
 * be checked against what real accounts report — a loop that ran for as long as
 * the wiki was the source and still left the structure wrong about ~100 slots.
 *
 * The cache holds what the game itself draws, so there is nothing to guess:
 *
 *     enum 2102          -> the five tab structs
 *     tab struct   p682  -> tab name          p683 -> enum of page structs
 *     page struct  p689  -> page name         p690 -> enum of item ids
 *     enum 3721          -> id replacements (see below)
 *
 * **The replacement enum is the whole point.** Some slots are stored against one
 * item id and *drawn* as another — the Coal bag's page entry is 12019 but the
 * log shows 25627, and the game remaps through enum 3721 before drawing. Script
 * 4100, which is how a client reports its slots, carries the drawn id, so the
 * structure has to hold the replaced ids or every one of those slots looks
 * unfillable. The community collection log plugin hardcodes twelve of these;
 * the cache currently lists fourteen, which is exactly why they are read rather
 * than copied.
 *
 * The cache is read over HTTP from abextm's public mirror, as
 * ``extract-ca-tasks.mjs`` does. Nothing is downloaded wholesale.
 *
 * Run (from disc/scripts/cache):
 *     npm install
 *     node extract-collection-log.mjs > ../collection_log_structure.json
 */
import * as cache from "@abextm/cache2";

const TAB_ENUM_ID = 2102;
const PARAM_TAB_NAME = 682;
const PARAM_TAB_PAGES_ENUM = 683;
const PARAM_PAGE_NAME = 689;
const PARAM_PAGE_ITEMS_ENUM = 690;
const REPLACEMENT_ENUM_ID = 3721;

const ref = process.env.OSRS_CACHE_COMMIT ?? "master";

/** The commit the mirror served, so the output says which game build it is. */
async function resolveRef() {
  try {
    const response = await fetch(
      `https://api.github.com/repos/abextm/osrs-cache/commits/${ref}`,
      { headers: { Accept: "application/vnd.github+json" } },
    );
    if (!response.ok) return null;
    const commit = await response.json();
    return { sha: commit.sha, date: commit.commit?.committer?.date ?? null };
  } catch {
    return null;
  }
}

const provider = new cache.FlatCacheProvider({
  getFile: async (name) => {
    const response = await fetch(
      `https://raw.githubusercontent.com/abextm/osrs-cache/${ref}/${name}`,
    );
    if (!response.ok) return undefined;
    return new Uint8Array(await response.arrayBuffer());
  },
});

function log(...args) {
  // stdout carries the JSON, so progress goes to stderr.
  console.error(...args);
}

/** Item id -> the id the log actually draws, for the slots that differ. */
async function loadReplacements() {
  const enumeration = await cache.Enum.load(provider, REPLACEMENT_ENUM_ID);
  const replacements = new Map();
  if (!enumeration) {
    throw new Error(`replacement enum ${REPLACEMENT_ENUM_ID} missing — has it moved?`);
  }
  for (const [from, to] of enumeration.map.entries()) {
    if (typeof from === "number" && typeof to === "number") replacements.set(from, to);
  }
  return replacements;
}

async function loadStruct(id) {
  const struct = await cache.Struct.load(provider, Number(id));
  if (!struct) throw new Error(`struct ${id} missing from the cache`);
  return struct;
}

async function loadEnum(id) {
  const enumeration = await cache.Enum.load(provider, Number(id));
  if (!enumeration) throw new Error(`enum ${id} missing from the cache`);
  return enumeration;
}

async function main() {
  const resolved = await resolveRef();
  if (resolved) log(`cache ${ref} = ${resolved.sha.slice(0, 12)} (${resolved.date})`);

  const replacements = await loadReplacements();
  log(`${replacements.size} item id replacements`);

  const tabsEnum = await loadEnum(TAB_ENUM_ID);
  const tabs = [];
  const itemIds = new Set();
  let slotCount = 0;

  for (const tabStructId of tabsEnum.map.values()) {
    const tabStruct = await loadStruct(tabStructId);
    const tabName = tabStruct.params.get(PARAM_TAB_NAME);
    const pagesEnumId = tabStruct.params.get(PARAM_TAB_PAGES_ENUM);
    if (typeof tabName !== "string" || typeof pagesEnumId !== "number") {
      throw new Error(`tab struct ${tabStructId} is not shaped like a tab`);
    }

    const pages = [];
    for (const pageStructId of (await loadEnum(pagesEnumId)).map.values()) {
      const pageStruct = await loadStruct(pageStructId);
      const pageName = pageStruct.params.get(PARAM_PAGE_NAME);
      const itemsEnumId = pageStruct.params.get(PARAM_PAGE_ITEMS_ENUM);
      if (typeof pageName !== "string" || typeof itemsEnumId !== "number") {
        throw new Error(`page struct ${pageStructId} is not shaped like a page`);
      }

      const items = [];
      for (const rawId of (await loadEnum(itemsEnumId)).map.values()) {
        const itemId = replacements.get(Number(rawId)) ?? Number(rawId);
        items.push(itemId);
        itemIds.add(itemId);
      }
      slotCount += items.length;
      pages.push({ name: pageName, struct: Number(pageStructId), items });
    }
    log(`${tabName}: ${pages.length} pages`);
    tabs.push({ name: tabName, struct: Number(tabStructId), pages });
  }

  // Names last, and only for the ids actually used, so this stays one pass over
  // the item archives rather than one over the whole cache.
  log(`naming ${itemIds.size} distinct items…`);
  const names = new Map();
  for (const itemId of itemIds) {
    const item = await cache.Item.load(provider, itemId);
    if (item?.name && item.name !== "null") names.set(itemId, item.name);
  }
  log(`  named ${names.size}`);

  for (const tab of tabs) {
    for (const page of tab.pages) {
      page.names = page.items.map((id) => names.get(id) ?? "");
    }
  }

  log(`total: ${slotCount} slots, ${itemIds.size} distinct ids`);
  process.stdout.write(JSON.stringify({
    cache_ref: ref,
    cache_commit: resolved?.sha ?? null,
    cache_commit_date: resolved?.date ?? null,
    slot_count: slotCount,
    distinct_items: itemIds.size,
    replacements: Object.fromEntries(replacements),
    tabs,
  }));
}

main().catch((error) => {
  console.error("extraction failed:", error);
  process.exitCode = 1;
});
