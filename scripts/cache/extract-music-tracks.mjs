/**
 * Extract the music track registry from the OSRS game cache (suggestion #252).
 *
 * The cache's `music` dbtable (44) has one row per track. Its `variable`
 * column is `[n, bit]`: the track is unlocked when bit `bit` of the varplayer
 * named `musicmulti_<n>` is set. So the plugin only has to send those varps raw
 * (the manifest's `music_varps`), and the server decodes them against this
 * file, the same split as the combat achievement varps.
 *
 * Output: {"varps": [varp ids, musicmulti_1 first], "tracks": [{id, name,
 * varp, bit, automatic, hidden, holiday, secondary}]}. Tracks with no unlock
 * variable are left out: nothing records them, so they cannot be counted.
 *
 * Run (from disc/scripts/cache):
 *     node extract-music-tracks.mjs > ../music_tracks.json
 */
import * as cache from "@abextm/cache2";

const MUSIC_TABLE = 44;
const COL = { name: 1, variable: 5, automatic: 6, hidden: 9, holiday: 10, secondary: 11 };
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

const varpByIndex = new Map();
for (const [id, gameVal] of await cache.GameVal.all(provider, VARPLAYER_GAMEVAL_CATEGORY)) {
  const match = /^musicmulti_(\d+)$/.exec(gameVal.name);
  if (match) varpByIndex.set(Number(match[1]), id);
}
if (!varpByIndex.size) throw new Error("no musicmulti_* varplayers found");
const maxIndex = Math.max(...varpByIndex.keys());
for (let i = 1; i <= maxIndex; i++) {
  if (!varpByIndex.has(i)) throw new Error(`musicmulti_${i} missing: the varp list has a gap`);
}

const first = (row, col) => (row.values[col] ? row.values[col][0] : null);
const tracks = [];
for (const row of await cache.DBRow.all(provider)) {
  if (row.table !== MUSIC_TABLE) continue;
  const variable = row.values[COL.variable];
  if (!variable) continue;
  const [index, bit] = variable;
  const varp = varpByIndex.get(index);
  if (varp === undefined) throw new Error(`track ${row.id} points at unknown musicmulti_${index}`);
  tracks.push({
    id: row.id,
    name: first(row, COL.name),
    varp,
    bit,
    automatic: first(row, COL.automatic) != null,
    hidden: first(row, COL.hidden) != null,
    holiday: first(row, COL.holiday) != null,
    secondary: first(row, COL.secondary) != null,
  });
}
tracks.sort((a, b) => a.id - b.id);
console.error(`${tracks.length} tracks over ${maxIndex} varps`);
const varps = [];
for (let i = 1; i <= maxIndex; i++) varps.push(varpByIndex.get(i));
process.stdout.write(JSON.stringify({ varps, tracks }, null, 1) + "\n");
