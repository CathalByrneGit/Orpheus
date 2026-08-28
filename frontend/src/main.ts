/**
 * The map's entry point.
 *
 * The payload is server-rendered into the page rather than fetched: the server
 * has already called `/graph/map` to decide what this actor may see, so a
 * second round trip would only add a flash of an empty frame and a second
 * chance for the two answers to differ.
 */
import { mount } from "svelte";
import Map from "./lib/Map.svelte";
import type { MapPayload } from "./lib/graph";

const host = document.getElementById("orpheus-map");
const raw = document.getElementById("orpheus-map-data")?.textContent;

if (host && raw) {
  const payload = JSON.parse(raw) as MapPayload;
  mount(Map, {
    target: host,
    props: {
      nodes: payload.nodes,
      edges: payload.edges,
      centre: host.dataset.centre || null,
      wikiPath: host.dataset.wikiPath ?? "/-/orpheus/wiki/",
      mapPath: host.dataset.mapPath ?? "/-/orpheus/map",
    },
  });
}
