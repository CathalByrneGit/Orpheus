/**
 * Colour for the map.
 *
 * The Okabe-Ito qualitative set, which stays distinguishable under the common
 * forms of colour-vision deficiency. Taken from datasette-paper's link graph,
 * whose reasoning applies here unchanged: these sit on SVG fills, so they are
 * fixed literals rather than theme tokens.
 *
 * The eighth entry is a neutral grey and is deliberately last: it doubles as
 * the "everything else" bucket for types beyond the seven distinct hues.
 */
export const OKABE_ITO = [
  "#0072B2", // blue
  "#E69F00", // orange
  "#009E73", // bluish green
  "#CC79A7", // reddish purple
  "#56B4E9", // sky blue
  "#D55E00", // vermillion
  "#F0E442", // yellow
  "#999999", // neutral grey -- overflow bucket
] as const;

export const OVERFLOW_COLOUR = "#999999";
const DISTINCT = OKABE_ITO.slice(0, 7);

/**
 * A colour per type, assigned by sorted position so a type keeps its hue
 * across reloads and between the whole-corpus map and an ego view of it. A
 * type that changed colour when the view narrowed would be read as a
 * different type.
 */
export function coloursForTypes(types: Iterable<string>): Map<string, string> {
  const sorted = [...new Set(types)].sort();
  const out = new Map<string, string>();
  sorted.forEach((type, i) => {
    out.set(type, i < DISTINCT.length ? DISTINCT[i] : OVERFLOW_COLOUR);
  });
  return out;
}
