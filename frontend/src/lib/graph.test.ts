import { describe, expect, it } from "vitest";
import {
  clampScale,
  fitTransform,
  isDimmed,
  isUnchecked,
  isVisible,
  isolates,
  movedBeyondThreshold,
  neighboursOf,
  radiusFor,
  screenToGraph,
  shouldAnimate,
  type MapEdge,
  type MapNode,
} from "./graph";
import { coloursForTypes, OVERFLOW_COLOUR } from "./palette";

const node = (id: string, degree = 0, type = "Company"): MapNode => ({
  entity_id: id,
  canonical_name: `Page ${id}`,
  type_id: type,
  status: "unconfirmed",
  degree,
});

// A -- B -- C, and D joined to nothing.
const EDGES = [
  { from_entity_id: "A", to_entity_id: "B", n_confirmed: 0 },
  { from_entity_id: "B", to_entity_id: "C", n_confirmed: 2 },
] as unknown as MapEdge[];

describe("what the store said", () => {
  it("calls a relation unchecked only when no source confirmed it", () => {
    expect(isUnchecked(EDGES[0])).toBe(true);
    expect(isUnchecked(EDGES[1])).toBe(false);
  });

  it("counts a page joined to nothing as an isolate", () => {
    const nodes = [node("A", 1), node("B", 2), node("D", 0)];
    expect(isolates(nodes).map((n) => n.entity_id)).toEqual(["D"]);
  });

  it("reads neighbours in either direction and never itself", () => {
    expect([...neighboursOf("B", EDGES)].sort()).toEqual(["A", "C"]);
    expect(neighboursOf("A", EDGES).has("A")).toBe(false);
  });
});

describe("what is drawn", () => {
  it("dims nothing until something is selected", () => {
    expect(isDimmed("A", null, new Set())).toBe(false);
  });

  it("dims everything but the selection and its neighbours", () => {
    const near = neighboursOf("B", EDGES);
    expect(isDimmed("B", "B", near)).toBe(false);
    expect(isDimmed("A", "B", near)).toBe(false);
    expect(isDimmed("D", "B", near)).toBe(true);
  });

  it("dims nothing when the selection has no neighbours to pick out", () => {
    expect(isDimmed("A", "D", new Set())).toBe(false);
    expect(isDimmed("D", "D", new Set())).toBe(false);
  });

  it("hides a type the legend switched off", () => {
    expect(isVisible(node("A", 1), new Set(["Company"]), false)).toBe(false);
    expect(isVisible(node("A", 1), new Set(["Person"]), false)).toBe(true);
  });

  it("hides isolates only when asked, and never a joined page", () => {
    expect(isVisible(node("D", 0), new Set(), true)).toBe(false);
    expect(isVisible(node("D", 0), new Set(), false)).toBe(true);
    expect(isVisible(node("A", 1), new Set(), true)).toBe(true);
  });

  it("keeps an unjoined page big enough to hit, and caps a hub", () => {
    expect(radiusFor(0)).toBe(5);
    expect(radiusFor(100)).toBe(18);
    expect(radiusFor(2)).toBeGreaterThan(radiusFor(1));
  });
});

describe("the view", () => {
  it("clamps zoom to the allowed range", () => {
    expect(clampScale(99)).toBe(4);
    expect(clampScale(0.001)).toBe(0.2);
    expect(clampScale(1.5)).toBe(1.5);
  });

  it("treats a small movement as a click and a large one as a drag", () => {
    expect(movedBeyondThreshold(1, 1)).toBe(false);
    expect(movedBeyondThreshold(30, 0)).toBe(true);
  });

  it("inverts the transform it was drawn with", () => {
    const t = { k: 2, x: 100, y: 50 };
    expect(screenToGraph(300, 150, t)).toEqual({ x: 100, y: 50 });
  });

  it("fits every point inside the box", () => {
    const points = [
      { x: 0, y: 0 },
      { x: 1000, y: 600 },
      { x: 500, y: 300 },
    ];
    const t = fitTransform(points, 800, 500);
    for (const p of points) {
      const sx = p.x * t.k + t.x;
      const sy = p.y * t.k + t.y;
      expect(sx).toBeGreaterThanOrEqual(0);
      expect(sx).toBeLessThanOrEqual(800);
      expect(sy).toBeGreaterThanOrEqual(0);
      expect(sy).toBeLessThanOrEqual(500);
    }
  });

  it("does not blow a tiny ego view up to fill the screen", () => {
    const t = fitTransform([{ x: 0, y: 0 }, { x: 10, y: 10 }], 800, 500);
    expect(t.k).toBeLessThanOrEqual(2.2);
  });

  it("survives an empty graph rather than returning NaN", () => {
    expect(fitTransform([], 800, 500)).toEqual({ k: 1, x: 0, y: 0 });
  });

  it("settles synchronously when motion is reduced or frames are absent", () => {
    expect(shouldAnimate(true, false)).toBe(true);
    expect(shouldAnimate(true, true)).toBe(false);
    expect(shouldAnimate(false, false)).toBe(false);
  });
});

describe("colour", () => {
  it("gives a type the same hue however many types are in view", () => {
    const whole = coloursForTypes(["Person", "Company", "Clause"]);
    const narrowed = coloursForTypes(["Person", "Company", "Clause"]);
    expect(narrowed.get("Person")).toBe(whole.get("Person"));
  });

  it("assigns by sorted position, not by order of appearance", () => {
    const a = coloursForTypes(["Person", "Company"]);
    const b = coloursForTypes(["Company", "Person"]);
    expect(a.get("Company")).toBe(b.get("Company"));
  });

  it("puts everything past the distinct hues in one grey bucket", () => {
    const many = coloursForTypes("abcdefghij".split(""));
    expect(many.get("h")).toBe(OVERFLOW_COLOUR);
    expect(many.get("a")).not.toBe(OVERFLOW_COLOUR);
  });
});
