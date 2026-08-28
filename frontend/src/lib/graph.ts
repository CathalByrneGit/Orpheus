/**
 * The shape `/graph/map` returns, and the pure functions over it.
 *
 * Everything here is DOM-free and d3-free so it can be pinned in a unit test.
 * The rules it encodes are Orpheus's, not the drawing's: what counts as
 * confirmed, what a page joined to nothing is, and what the reader is allowed
 * to conclude from the picture.
 */

export interface MapNode {
  entity_id: string;
  canonical_name: string;
  type_id: string;
  status: string;
  degree: number;
}

export interface MapEdge {
  from_entity_id: string;
  to_entity_id: string;
  from_name: string;
  to_name: string;
  link_type_id: string;
  n_documents: number;
  n_sources: number;
  n_confirmed: number;
  max_confidence: number;
}

export interface MapPayload {
  nodes: MapNode[];
  edges: MapEdge[];
}

/**
 * A relation nobody has checked. `n_confirmed` counts the sources whose review
 * status is confirmed or amended, so zero means every document asserting this
 * relation is still an unreviewed extraction -- which the map must not draw
 * the same as a relation a person has stood behind.
 */
export function isUnchecked(edge: Pick<MapEdge, "n_confirmed">): boolean {
  return !edge.n_confirmed;
}

/**
 * Pages joined to nothing that reached the graph. In a young corpus these are
 * most of it, and they crowd out the shape the map exists to show -- but they
 * are real pages, so hiding them is the reader's choice and the control says
 * how many it is hiding.
 */
export function isolates(nodes: MapNode[]): MapNode[] {
  return nodes.filter((n) => !n.degree);
}

/** The neighbours of one page, by id. Excludes the page itself. */
export function neighboursOf(entityId: string, edges: MapEdge[]): Set<string> {
  const out = new Set<string>();
  for (const e of edges) {
    if (e.from_entity_id === entityId) out.add(e.to_entity_id);
    if (e.to_entity_id === entityId) out.add(e.from_entity_id);
  }
  out.delete(entityId);
  return out;
}

/**
 * Is this node dimmed? Only when something is selected and this is neither it
 * nor one of its neighbours. With nothing selected nothing is dimmed: a map
 * that greys most of itself on load reads as a map of almost nothing.
 */
export function isDimmed(
  entityId: string,
  selected: string | null,
  neighbours: Set<string>,
): boolean {
  if (selected === null) return false;
  // Dimming exists to pick out a neighbourhood. A page joined to nothing has
  // none, and greying all 147 others to say so tells the reader nothing the
  // panel does not say in words -- it just hides the map.
  if (neighbours.size === 0) return false;
  if (entityId === selected) return false;
  return !neighbours.has(entityId);
}

/** Is this node drawn at all, given the type filter and the isolate toggle? */
export function isVisible(
  node: MapNode,
  hiddenTypes: Set<string>,
  hideIsolates: boolean,
): boolean {
  if (hiddenTypes.has(node.type_id)) return false;
  return !(hideIsolates && !node.degree);
}

/**
 * Node radius from the number of relations, in screen pixels.
 *
 * Floored, not proportional: a page joined to nothing is the commonest thing
 * in a young corpus and still has to be a dot somebody can hit. Capped so one
 * hub does not swallow the picture.
 */
export function radiusFor(degree: number): number {
  return Math.min(5 + degree * 1.6, 18);
}

/**
 * Node radius on screen, given how far the view is zoomed out.
 *
 * Two failures sit either side of this. Sizing in graph coordinates renders
 * 159 pages as dust the moment the fit zooms out, and blows them into blobs
 * when it zooms in. Holding a constant screen size fixes both and then cannot
 * be improved on: the fit normalises away any change to the layout, so no
 * amount of repulsion separates nodes that are 10 pixels wide in a frame that
 * has to hold all of them -- measured on the calibration corpus, three very
 * different force settings all landed within 30 of the same overlap count.
 *
 * So the size follows the zoom, but only part of the way and never below a
 * floor. Zoomed out the corpus reads as a shape; zoomed in it reads as pages.
 */
export function screenRadius(degree: number, scale: number, floor = 0.45): number {
  return radiusFor(degree) * Math.min(1, Math.max(floor, scale));
}

const K_MIN = 0.2;
const K_MAX = 4;

/** Clamp a zoom factor to what the view allows. */
export function clampScale(k: number): number {
  return Math.min(K_MAX, Math.max(K_MIN, k));
}

/**
 * A pointer that has travelled this far is dragging, so the click that follows
 * is a reposition and not a selection. Without it, nudging a node opens it.
 */
export function movedBeyondThreshold(dx: number, dy: number, threshold = 4): boolean {
  return Math.hypot(dx, dy) > threshold;
}

/** Screen pixel to graph coordinate, inverting the `<g>` transform. */
export function screenToGraph(
  px: number,
  py: number,
  transform: { k: number; x: number; y: number },
): { x: number; y: number } {
  return { x: (px - transform.x) / transform.k, y: (py - transform.y) / transform.k };
}

/**
 * The transform that fits laid-out points into a box.
 *
 * A relaxation has no idea how big the viewport is, so without this the corpus
 * settles to whatever width the repulsion wants and half of it sits outside
 * the frame -- which reads as "there is nothing over there" rather than "you
 * cannot see it". Scale is capped so a two-node ego view is not blown up to
 * fill a screen.
 */
export function fitTransform(
  points: { x: number; y: number }[],
  width: number,
  height: number,
  pad = 40,
  maxScale = 2.2,
): { k: number; x: number; y: number } {
  if (!points.length) return { k: 1, x: 0, y: 0 };
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(...ys);
  const y1 = Math.max(...ys);
  const k = Math.min(
    (width - pad * 2) / Math.max(x1 - x0, 1),
    (height - pad * 2) / Math.max(y1 - y0, 1),
    maxScale,
  );
  return {
    k,
    x: pad - x0 * k + (width - pad * 2 - (x1 - x0) * k) / 2,
    y: pad - y0 * k + (height - pad * 2 - (y1 - y0) * k) / 2,
  };
}

/**
 * Should the simulation animate? Only when the platform can drive frames and
 * the viewer has not asked for less motion; otherwise the layout is settled in
 * one synchronous pass and drawn once.
 */
export function shouldAnimate(hasRaf: boolean, reduceMotion: boolean): boolean {
  return hasRaf && !reduceMotion;
}


/**
 * The degree at which a node earns a permanent label.
 *
 * A fixed threshold does not survive a change in what the corpus contains.
 * Giving contracts pages turned 57 relations into 175 and took the map from 8
 * labels to 121 -- past the point where any of them could be read, because
 * they overlapped each other. This keeps roughly `most` of them: the highest
 * degrees, whatever those are in this graph. Everything else names itself when
 * the pointer is on it or when it is selected, so nothing becomes unreachable
 * -- only quiet.
 */
export function labelThreshold(
  degrees: number[],
  most = 24,
  floor = 2,
): number {
  const ranked = degrees.filter((d) => d >= floor).sort((a, b) => b - a);
  if (ranked.length <= most) return floor;
  // The degree of the `most`-th node, so ties above it are all kept rather
  // than cut arbitrarily mid-tie.
  return ranked[most - 1];
}
