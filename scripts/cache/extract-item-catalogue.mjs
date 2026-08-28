/**
 * Extract every item's name and stack-variant table from the OSRS game cache.
 *
 * Two long-standing gaps close with one artifact, and both come from the same
 * mistake — treating the ``items`` table as if it were a catalogue. It is not:
 * it only holds what somebody has *submitted*, so it knows nothing about an
 * item until it has been dropped, and 89 of the 95 gear ids on personal-best
 * loadouts had no row in it at all.
 *
 * **Names.** A tooltip driven off ``items`` reads "Item 30000" for exactly the
 * items that were already rendering wrong. The cache names all of them
 * (30000 is a Chugging barrel, 30805 a Dossier, 32391 a Medallion fragment).
 *
 * **Stack variants.** A stackable item is stored as its single-unit id, and the
 * game swaps to progressively larger pile sprites as the stack grows. The cache
 * carries that mapping per item, as parallel ``stackVariantQuantities`` and
 * ``stackVariantItems`` arrays — for coins, thresholds 2/3/4/5/25/100/250/
 * 1000/10000 against ids 996..1004.
 *
 * Reading it rather than hardcoding it matters: the hand-written coin mapping
 * this replaces had 10 -> 1000, 50 -> 1001 and 100 -> 1002, where the game
 * actually switches at 25, 100 and 250. A stack of 100 coins was drawn with the
 * 250-pile sprite.
 *
 * The cache is read over HTTP from abextm's public mirror, exactly as
 * ``extract-collection-log.mjs`` and ``extract-ca-tasks.mjs`` do.
 *
 * Run (from disc/scripts/cache):
 *     npm install
 *     node extract-item-catalogue.mjs > ../item_catalogue.json
 */
import * as cache from "@abextm/cache2";

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

/**
 * The item's stack thresholds as [quantity, itemId] pairs, ascending.
 *
 * The cache pads both arrays to a fixed width with zeroes; a zero item id means
 * "no variant here", not "item 0", so the padding has to be dropped or every
 * stackable would appear to have a variant that renders nothing.
 */
function stackVariants(item) {
  const ids = item.stackVariantItems ?? [];
  const quantities = item.stackVariantQuantities ?? [];
  const pairs = [];
  for (let i = 0; i < Math.min(ids.length, quantities.length); i += 1) {
    const id = Number(ids[i]);
    const quantity = Number(quantities[i]);
    if (!id || !quantity || quantity < 2) continue;
    pairs.push([quantity, id]);
  }
  pairs.sort((a, b) => a[0] - b[0]);
  return pairs;
}

const commit = await resolveRef();
const all = Array.from(await cache.Item.all(provider));

/** The item's own name, or "" when the cache does not give it one. */
function ownName(item) {
  const name = typeof item?.name === "string" ? item.name.trim() : "";
  // The cache spells "no name" as the literal string "null".
  return !name || name === "null" ? "" : name;
}

const byId = new Map(all.map((item) => [item.id, item]));

/**
 * The name the game would show for an item.
 *
 * Noted and placeholder items carry no name of their own — the cache leaves it
 * "null" and points at the item they stand in for (a note via
 * ``noteLinkedItem``, a bank placeholder via ``placeholderLinkedItem``), which
 * is where the game reads the name from. Resolving the link matters more than
 * it sounds: noted items are ~5% of the ids that appear on real personal-best
 * loadouts, so skipping them would leave a tooltip blank on precisely the
 * inventory entries players carry most (noted potions, herbs, food).
 */
function displayName(item, depth = 0) {
  const own = ownName(item);
  if (own) return own;
  // Guard the walk: a malformed link cycle must not hang the extractor.
  if (depth > 4) return "";
  for (const linkedId of [item?.noteLinkedItem, item?.placeholderLinkedItem]) {
    if (typeof linkedId === "number" && linkedId >= 0 && linkedId !== item.id) {
      const linked = byId.get(linkedId);
      if (linked) {
        const name = displayName(linked, depth + 1);
        if (name) return name;
      }
    }
  }
  return "";
}

const items = {};
let named = 0;
let viaLink = 0;
let withVariants = 0;
for (const item of all) {
  const name = displayName(item);
  // The cache defines far more ids than the game names; a still-unnamed id is
  // an internal slot, and carrying it would bloat the file with entries that
  // can never be displayed.
  if (!name) continue;
  if (!ownName(item)) viaLink += 1;
  const entry = { n: name };
  const variants = stackVariants(item);
  if (variants.length) {
    entry.sv = variants;
    withVariants += 1;
  }
  items[item.id] = entry;
  named += 1;
}

process.stdout.write(
  `${JSON.stringify(
    {
      cache_ref: ref,
      cache_commit: commit?.sha ?? null,
      cache_commit_date: commit?.date ?? null,
      defined: all.length,
      named,
      named_via_link: viaLink,
      with_stack_variants: withVariants,
      items,
    },
    null,
    0,
  )}\n`,
);
