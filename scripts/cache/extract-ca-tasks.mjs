/**
 * Extract the combat achievement task registry from the OSRS game cache.
 *
 * Why the cache and not the wiki: the wiki publishes task names, tiers and
 * monsters, but nothing that ties a task to the varp bit that records whether
 * it is done. The cache has both — a task struct carries its varp *index*
 * (param 1306), and the completion varps are ordered by their `ca_task_completed_N`
 * gameval name, so:
 *
 *     varp = orderedVarps[index >> 5]   bit = index & 31
 *
 * That is the join that lets stored varps be decoded into named tasks. It also
 * means no plugin change is needed: the client already sends the raw varps.
 *
 * The cache is read over HTTP from abextm's public mirror, the same source
 * RuneProfile's own scripts use. Nothing is downloaded wholesale; cache2 fetches
 * only the groups it touches.
 *
 * Run (from disc/scripts/cache):
 *     npm install
 *     node extract-ca-tasks.mjs > ../ca_tasks.json
 */
import * as cache from "@abextm/cache2";

// Enums listing the task structs of each tier, in tier order.
const TIER_ENUM_IDS = [3981, 3982, 3983, 3984, 3985, 3986];
const TIER_NAMES = ["Easy", "Medium", "Hard", "Elite", "Master", "Grandmaster"];

// Task struct params.
const PARAM_VARP_INDEX = 1306;
const PARAM_NAME = 1308;
const PARAM_DESCRIPTION = 1309;
const PARAM_TYPE = 1311;
const PARAM_MONSTER = 1312;

// Lookup enums: monster category -> display name, task type -> display name.
const MONSTER_ENUM_ID = 3971;
const TYPE_ENUM_ID = 3969;

// Gameval category holding varplayer names.
const VARPLAYER_GAMEVAL_CATEGORY = 3;

const ref = process.env.OSRS_CACHE_COMMIT ?? "master";

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

/**
 * The completion varps, in the order that defines task indices.
 *
 * Ordering is load-bearing: a task's index is a position in this list times 32
 * plus a bit. A gap would silently shift every task after it onto the wrong
 * varp, so a non-contiguous run is a hard error rather than a warning.
 */
async function loadOrderedVarps() {
  const varplayers = await cache.GameVal.all(provider, VARPLAYER_GAMEVAL_CATEGORY);
  const entries = [];
  for (const [id, gameVal] of varplayers) {
    const match = /^ca_task_completed_(\d+)$/.exec(gameVal.name);
    if (match) entries.push({ id, position: Number(match[1]) });
  }
  entries.sort((a, b) => a.position - b.position);

  if (!entries.length) {
    throw new Error("no ca_task_completed_* varplayers found — did the gameval category move?");
  }
  entries.forEach(({ position }, i) => {
    if (position !== i) {
      throw new Error(`varp positions are not contiguous at ${i} (found ${position})`);
    }
  });
  return entries.map((e) => e.id);
}

async function loadNameEnum(id) {
  const enumeration = await cache.Enum.load(provider, id);
  const names = new Map();
  if (!enumeration) return names;
  for (const [key, value] of enumeration.map.entries()) {
    if (typeof key === "number" && typeof value === "string") names.set(key, value);
  }
  return names;
}

async function main() {
  const varps = await loadOrderedVarps();
  log(`ordered completion varps: ${varps.length} (${varps[0]}..${varps[varps.length - 1]})`);

  const monsters = await loadNameEnum(MONSTER_ENUM_ID);
  const types = await loadNameEnum(TYPE_ENUM_ID);
  log(`monster categories: ${monsters.size}, task types: ${types.size}`);

  const tasks = [];
  for (let tierIdx = 0; tierIdx < TIER_ENUM_IDS.length; tierIdx++) {
    const tierEnum = await cache.Enum.load(provider, TIER_ENUM_IDS[tierIdx]);
    if (!tierEnum) {
      log(`tier enum ${TIER_ENUM_IDS[tierIdx]} missing; skipping`);
      continue;
    }

    let count = 0;
    for (const structId of tierEnum.map.values()) {
      const struct = await cache.Struct.load(provider, Number(structId));
      if (!struct) continue;

      const index = struct.params.get(PARAM_VARP_INDEX);
      const name = struct.params.get(PARAM_NAME);
      if (typeof index !== "number" || typeof name !== "string") continue;

      const varpSlot = index >> 5;
      if (varpSlot >= varps.length) {
        log(`task "${name}" has index ${index}, beyond the ${varps.length} known varps; skipping`);
        continue;
      }

      tasks.push({
        index,
        // The pair that decodes a stored varp into this task.
        varp: varps[varpSlot],
        bit: index & 31,
        tier: TIER_NAMES[tierIdx],
        tier_id: tierIdx + 1,
        name,
        description: struct.params.get(PARAM_DESCRIPTION) ?? "",
        type: types.get(struct.params.get(PARAM_TYPE)) ?? "",
        monster: monsters.get(struct.params.get(PARAM_MONSTER)) ?? "",
      });
      count++;
    }
    log(`${TIER_NAMES[tierIdx]}: ${count} tasks`);
  }

  tasks.sort((a, b) => a.index - b.index);

  const duplicates = tasks.length - new Set(tasks.map((t) => t.index)).size;
  if (duplicates) throw new Error(`${duplicates} tasks share a varp index`);

  log(`total: ${tasks.length} tasks across ${new Set(tasks.map((t) => t.monster)).size} monsters`);
  process.stdout.write(JSON.stringify({ varps, tasks }));
}

main().catch((error) => {
  console.error("extraction failed:", error);
  process.exitCode = 1;
});
