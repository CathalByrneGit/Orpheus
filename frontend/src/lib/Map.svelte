<script lang="ts">
  import {
    forceCenter,
    forceCollide,
    forceLink,
    forceManyBody,
    forceSimulation,
    type Simulation,
    type SimulationLinkDatum,
    type SimulationNodeDatum,
  } from "d3-force";
  import { onDestroy, onMount, untrack } from "svelte";
  import {
    clampScale,
    fitTransform,
    isDimmed,
    isUnchecked,
    isVisible,
    movedBeyondThreshold,
    neighboursOf,
    radiusFor,
    screenToGraph,
    shouldAnimate,
    type MapEdge,
    type MapNode,
  } from "./graph";
  import { coloursForTypes } from "./palette";

  interface Props {
    nodes: MapNode[];
    edges: MapEdge[];
    /** The page an ego view is centred on, or null for the whole corpus. */
    centre: string | null;
    /** Where a page lives, so the panel can link to it. */
    wikiPath: string;
    /** Where this page lives, so the panel can re-centre the map. */
    mapPath: string;
  }

  let { nodes, edges, centre, wikiPath, mapPath }: Props = $props();

  type SimNode = MapNode & SimulationNodeDatum;
  type SimEdge = SimulationLinkDatum<SimNode> & MapEdge;

  const HEIGHT = 560;
  const TICKS = 300;

  // The payload is fixed for the life of the page -- the server rendered it,
  // and narrowing the view is a new request, not a prop change. `untrack` says
  // that the initial value is the one meant, rather than leaving a warning
  // that suggests these should have been reactive.
  const initial = untrack(() => ({ nodes, edges }));

  const colours = coloursForTypes(initial.nodes.map((n) => n.type_id));
  const types = [...colours.keys()];
  const alone = initial.nodes.filter((n) => !n.degree).length;

  let frame: HTMLDivElement;
  let width = $state(900);
  let hiddenTypes = $state(new Set<string>());
  let hideIsolates = $state(false);
  let selected = $state<MapNode | null>(null);
  let transform = $state({ k: 1, x: 0, y: 0 });
  // Positions live outside Svelte's reactive graph: d3 mutates them every tick
  // and a proxy per node makes the simulation crawl. The tick handler bumps
  // one counter, and the template reads positions through it.
  let ticked = $state(0);

  const simNodes: SimNode[] = initial.nodes.map((n) => ({ ...n }));
  const byId = new Map(simNodes.map((n) => [n.entity_id, n]));
  const simEdges: SimEdge[] = initial.edges
    .filter((e) => byId.has(e.from_entity_id) && byId.has(e.to_entity_id))
    .map((e) => ({
      ...e,
      source: byId.get(e.from_entity_id)!,
      target: byId.get(e.to_entity_id)!,
    }));

  let sim: Simulation<SimNode, SimEdge> | null = null;
  let animated = true;

  const near = $derived(selected ? neighboursOf(selected.entity_id, edges) : new Set<string>());
  const shown = $derived.by(() => {
    // `ticked` is read so the derivation reruns as the layout moves.
    ticked;
    return simNodes.filter((n) => isVisible(n, hiddenTypes, hideIsolates));
  });
  const shownEdges = $derived.by(() => {
    ticked;
    return simEdges.filter(
      (e) =>
        isVisible(e.source as SimNode, hiddenTypes, hideIsolates) &&
        isVisible(e.target as SimNode, hiddenTypes, hideIsolates),
    );
  });

  // A selection that is filtered out of view leaves the panel describing a page
  // nobody can see, with the map dimmed around a node that is not drawn.
  $effect(() => {
    if (selected && !isVisible(selected, hiddenTypes, hideIsolates)) selected = null;
  });

  function fit() {
    const live = simNodes.filter((n) => isVisible(n, hiddenTypes, hideIsolates));
    if (!live.length) return;
    transform = fitTransform(
      live.map((n) => ({ x: n.x ?? 0, y: n.y ?? 0 })),
      width,
      HEIGHT,
    );
  }

  onMount(() => {
    width = frame.clientWidth || width;
    const observer = new ResizeObserver(() => {
      width = frame.clientWidth || width;
    });
    observer.observe(frame);

    const reduce =
      typeof matchMedia !== "function" ||
      matchMedia("(prefers-reduced-motion: reduce)").matches;
    animated = shouldAnimate(typeof requestAnimationFrame === "function", reduce);

    sim = forceSimulation<SimNode>(simNodes)
      .force("charge", forceManyBody().strength(-220))
      .force(
        "link",
        forceLink<SimNode, SimEdge>(simEdges)
          .id((n) => n.entity_id)
          .distance(90),
      )
      .force("collide", forceCollide(24))
      .force("centre", forceCenter(width / 2, HEIGHT / 2));

    if (animated) {
      sim.on("tick", () => (ticked += 1));
      sim.on("end", fit);
    } else {
      // Reduced motion, or no frames to drive: settle in one pass and draw the
      // result. The picture is the same; it simply does not move on the way.
      sim.stop();
      for (let i = 0; i < TICKS; i++) sim.tick();
      ticked += 1;
      fit();
    }

    return () => observer.disconnect();
  });

  onDestroy(() => sim?.stop());

  function reheat(alpha = 0.4) {
    if (!sim) return;
    if (animated) {
      sim.alpha(alpha).restart();
    } else {
      for (let i = 0; i < TICKS; i++) sim.tick();
      ticked += 1;
      fit();
    }
  }

  function toggleType(type: string) {
    const next = new Set(hiddenTypes);
    next.has(type) ? next.delete(type) : next.add(type);
    hiddenTypes = next;
    reheat();
  }

  // -- pointer: drag a node, pan the canvas, zoom ---------------------------

  let dragging: SimNode | null = null;
  let panning: { x: number; y: number } | null = null;
  let travelled = { x: 0, y: 0 };

  function localPoint(event: PointerEvent) {
    const box = frame.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  }

  function startNode(event: PointerEvent, node: SimNode) {
    event.stopPropagation();
    (event.target as Element).setPointerCapture?.(event.pointerId);
    dragging = node;
    travelled = { x: 0, y: 0 };
    node.fx = node.x;
    node.fy = node.y;
    if (animated) sim?.alphaTarget(0.3).restart();
  }

  function startPan(event: PointerEvent) {
    panning = { x: event.clientX - transform.x, y: event.clientY - transform.y };
    travelled = { x: 0, y: 0 };
  }

  function move(event: PointerEvent) {
    if (dragging) {
      travelled = { x: travelled.x + event.movementX, y: travelled.y + event.movementY };
      const p = localPoint(event);
      const g = screenToGraph(p.x, p.y, transform);
      dragging.fx = g.x;
      dragging.fy = g.y;
      if (!animated) reheat(0);
      ticked += 1;
    } else if (panning) {
      travelled = { x: travelled.x + event.movementX, y: travelled.y + event.movementY };
      transform = { ...transform, x: event.clientX - panning.x, y: event.clientY - panning.y };
    }
  }

  function release() {
    if (dragging) {
      // The node stays where it was put. Releasing it back to the simulation
      // would undo the reader's arrangement the moment they let go.
      if (animated) sim?.alphaTarget(0);
    }
    dragging = null;
    panning = null;
  }

  function clickNode(event: MouseEvent, node: SimNode) {
    event.stopPropagation();
    // A pointer that travelled was arranging the map, not choosing a page.
    if (movedBeyondThreshold(travelled.x, travelled.y)) return;
    selected = selected?.entity_id === node.entity_id ? null : node;
  }

  function zoom(event: WheelEvent) {
    event.preventDefault();
    const box = frame.getBoundingClientRect();
    const px = event.clientX - box.left;
    const py = event.clientY - box.top;
    const before = screenToGraph(px, py, transform);
    const k = clampScale(transform.k * (event.deltaY < 0 ? 1.1 : 0.9));
    // Keep the point under the cursor under the cursor.
    transform = { k, x: px - before.x * k, y: py - before.y * k };
  }

  function keyNode(event: KeyboardEvent, node: SimNode) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selected = selected?.entity_id === node.entity_id ? null : node;
    }
  }

  const relationsOf = (node: MapNode) =>
    edges.filter(
      (e) => e.from_entity_id === node.entity_id || e.to_entity_id === node.entity_id,
    );
</script>

<div class="legend">
  {#if alone}
    <label>
      <input type="checkbox" id="hide-alone" bind:checked={hideIsolates} onchange={() => reheat()} />
      hide the {alone} page(s) joined to nothing
    </label>
  {/if}
  {#each types as type (type)}
    <label>
      <input
        type="checkbox"
        checked={!hiddenTypes.has(type)}
        onchange={() => toggleType(type)}
      />
      <span class="swatch" style:background={colours.get(type)}></span>
      {type}
    </label>
  {/each}
  <span class="key">faded = nobody has confirmed the page; dashed = nobody has confirmed the relation</span>
</div>

<div class="layout">
  <div class="canvas">
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div
      class="frame"
      bind:this={frame}
      role="application"
      aria-label="The corpus as a map"
      onpointerdown={startPan}
      onpointermove={move}
      onpointerup={release}
      onpointercancel={release}
      onwheel={zoom}
    >
      <svg width="100%" height={HEIGHT} role="presentation">
        <g transform="translate({transform.x},{transform.y}) scale({transform.k})">
          {#each shownEdges as edge (edge.from_entity_id + edge.link_type_id + edge.to_entity_id)}
            {@const source = edge.source as SimNode}
            {@const target = edge.target as SimNode}
            <line
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              stroke="#999"
              stroke-width={Math.min(1 + edge.n_documents * 0.4, 4) / transform.k}
              stroke-dasharray={isUnchecked(edge) ? `${4 / transform.k} ${3 / transform.k}` : undefined}
              opacity={selected && !(source.entity_id === selected.entity_id || target.entity_id === selected.entity_id) ? 0.15 : 0.85}
            />
          {/each}
          {#each shown as node (node.entity_id)}
            {@const dim = isDimmed(node.entity_id, selected?.entity_id ?? null, near)}
            {@const r = radiusFor(node.degree) / transform.k}
            <g opacity={dim ? 0.2 : 1}>
              <circle
                cx={node.x}
                cy={node.y}
                {r}
                fill={colours.get(node.type_id)}
                fill-opacity={node.status === "confirmed" ? 1 : 0.62}
                stroke={node.entity_id === centre ? "#111" : "#fff"}
                stroke-width={(node.entity_id === centre ? 3 : 1.5) / transform.k}
                role="button"
                tabindex="0"
                aria-label={node.canonical_name}
                onpointerdown={(e) => startNode(e, node)}
                onclick={(e) => clickNode(e, node)}
                onkeydown={(e) => keyNode(e, node)}
              />
              {#if node.degree >= 2 || node.entity_id === centre || node.entity_id === selected?.entity_id}
                <text
                  x={(node.x ?? 0) + r + 4 / transform.k}
                  y={(node.y ?? 0) + 4 / transform.k}
                  font-size={11 / transform.k}
                  stroke="#fcfcfc"
                  stroke-width={3 / transform.k}
                  paint-order="stroke"
                  fill="#333"
                  pointer-events="none"
                >{node.canonical_name.slice(0, 28)}</text>
              {/if}
            </g>
          {/each}
        </g>
      </svg>
    </div>
    <p class="hint">
      Drag to pan, scroll to zoom, drag a node to move it. Click a node for what the
      store holds about it. Size is the number of relations; a dashed edge is one
      nobody has confirmed.
    </p>
  </div>

  <div class="panel" id="panel">
    {#if selected}
      <h2>{selected.canonical_name}</h2>
      <p class="meta">
        {selected.type_id} &middot; {selected.status} &middot;
        {relationsOf(selected).length} relation(s)
      </p>
      <p>
        <a href="{wikiPath}{selected.entity_id}">the page</a> &middot;
        <a href="{mapPath}?entity={selected.entity_id}&depth=2">centre the map here</a>
      </p>
      {#if relationsOf(selected).length}
        <ul>
          {#each relationsOf(selected) as edge (edge.from_entity_id + edge.link_type_id + edge.to_entity_id)}
            {@const other =
              edge.from_entity_id === selected.entity_id
                ? { id: edge.to_entity_id, name: edge.to_name }
                : { id: edge.from_entity_id, name: edge.from_name }}
            <li>
              {edge.link_type_id} &rarr; <a href="{wikiPath}{other.id}">{other.name}</a><br />
              <span class="meta">
                {edge.n_documents} document(s),
                {edge.n_confirmed ? `${edge.n_confirmed} confirmed` : "nobody has checked this"}
              </span>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="meta">Related to nothing that reached the graph.</p>
      {/if}
    {:else}
      <p class="meta">Click a node.</p>
    {/if}
  </div>
</div>

<style>
  .legend {
    margin-bottom: 0.5em;
    font-size: 0.9em;
    display: flex;
    gap: 1em;
    flex-wrap: wrap;
    align-items: center;
  }
  .legend label {
    cursor: pointer;
  }
  .swatch {
    display: inline-block;
    width: 0.7em;
    height: 0.7em;
    border-radius: 50%;
    vertical-align: baseline;
  }
  .key {
    color: #666;
  }
  .layout {
    display: flex;
    gap: 1em;
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .canvas {
    flex: 1 1 34em;
    min-width: 22em;
  }
  .frame {
    border: 1px solid #ddd;
    background: #fcfcfc;
    cursor: grab;
    touch-action: none;
  }
  .frame:active {
    cursor: grabbing;
  }
  circle {
    cursor: pointer;
  }
  .panel {
    flex: 1 1 17em;
    min-width: 15em;
    border: 1px solid #ddd;
    padding: 1em;
    min-height: 12em;
  }
  .panel h2 {
    margin-top: 0;
  }
  .panel ul {
    padding-left: 1.1em;
  }
  .meta,
  .hint {
    color: #666;
  }
  .hint {
    margin: 0.4em 0;
  }
</style>
