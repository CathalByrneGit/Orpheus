/**
 * Orpheus references inside a datasette-paper document.
 *
 * Paste an Orpheus URL into a paper and it becomes a live pill: the thing's
 * name, and — the reason this exists — whether a person has actually checked
 * it. An unconfirmed machine reading quoted into a memo looks exactly like a
 * verified fact unless something says otherwise, and that is the one failure
 * this whole project is built to prevent. So the review state is not a detail
 * on the block card; it is on the inline pill, in the title, and in a class
 * the stylesheet colours.
 *
 * Everything is resolved against the *viewer's* cookie, per paper's contract:
 * two people open the same memo and each sees only what they may see. The
 * backend at /-/orpheus/api/refs/<id> keeps the leak discipline — `denied` and
 * `not_found` carry no label — and this file's only job on that front is never
 * to invent one.
 *
 * Written by hand rather than built: it is one small ES module with no imports,
 * and putting it through the Svelte/Vite pipeline would buy a content hash and
 * cost a build step on a file that changes once a year.
 */

const API = "/-/orpheus/api/refs";

// A ref is a path under /-/orpheus/. Three shapes are claimed: the canonical
// permalink the picker inserts, and the two human-facing pages somebody is
// likely to have open in another tab and paste.
const REF_PATH = /^\/-\/orpheus\/(?:ref|wiki|document)\/([a-z]+_[A-Za-z0-9]+)$/;

// Static markup, one per kind, and never built from resource data — paper
// renders `icon` as raw HTML on purpose (the bundle is already trusted code),
// which makes interpolating anything from the store into it the one way to
// turn that trust into an injection.
const ICONS = {
  document:
    '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4 1h5l3 3v11H4V1zm5 1v3h3"/></svg>',
  // A page, not a person: an entity is whatever type the bundle says, and a
  // Company wearing a person glyph is the card telling the reader something
  // false before they have read a word of it.
  entity:
    '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M3 2h10v12l-5-3-5 3z"/></svg>',
  instance:
    '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M2 4h12v2H2zm0 4h8v2H2zm0 4h10v2H2z"/></svg>',
  tension:
    '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 1l7 13H1zM7 6h2v4H7zm0 5h2v2H7z"/></svg>',
};

function idFrom(ref) {
  const m = REF_PATH.exec(ref || "");
  return m ? m[1] : null;
}

/** The backend's answer, or a refusal. Never invents a label. */
async function fetchCard(ref) {
  const id = idFrom(ref);
  if (!id) return { status: "not_found" };
  let res;
  try {
    res = await fetch(`${API}/${encodeURIComponent(id)}`, {
      headers: { accept: "application/json" },
      credentials: "same-origin",
    });
  } catch (e) {
    // A network failure is not an answer about the resource. Saying
    // "not_found" would tell the reader this thing does not exist, which we
    // have no idea about.
    return { status: "error" };
  }
  if (res.status === 401 || res.status === 403) return { status: "denied" };
  if (!res.ok) return { status: "error" };
  try {
    return await res.json();
  } catch (e) {
    return { status: "error" };
  }
}

/** One short phrase for the pill, so the state is visible without hovering. */
function stateSuffix(card) {
  const review = card.review || {};
  if (review.contested) return "in dispute";
  if (review.checked) return null; // the quiet case: a person has ruled on it
  if (card.kind === "tension") return "open";
  return "unchecked";
}

const provider = {
  kind: "orpheus-ref",

  matchRef: (ref) => REF_PATH.test(ref || ""),

  matchUrl: (url) => (REF_PATH.test(url.pathname) ? url.pathname : null),

  async resolve(ref) {
    const card = await fetchCard(ref);
    if (card.status === "denied") return { status: "denied" };
    if (card.status === "not_found") return { status: "not_found" };
    if (card.status !== "ok" && card.status !== "redacted")
      return { status: "not_found" };

    if (card.status === "redacted") {
      // Distinguished from `denied` deliberately: the row was kept so a reader
      // learns the document was removed rather than concluding it said nothing.
      return {
        status: "ok",
        kind: "orpheus-ref",
        label: "(redacted document)",
        href: ref,
        icon: ICONS.document,
      };
    }

    const suffix = stateSuffix(card);
    return {
      status: "ok",
      kind: "orpheus-ref",
      label: suffix ? `${card.label} · ${suffix}` : card.label,
      href: card.href || ref,
      icon: ICONS[card.kind] || ICONS.instance,
    };
  },

  mount(host, ctx) {
    // A distinct class from the card it will hold. Sharing one meant every
    // embed matched twice in the DOM -- found by counting them on a live page,
    // where five references rendered as ten elements.
    const root = document.createElement("div");
    root.className = "orpheus-ref-mount";
    root.textContent = "Resolving…";
    host.appendChild(root);

    let live = true;
    fetchCard(ctx.ref).then((card) => {
      if (!live) return;
      root.textContent = "";
      root.appendChild(renderCard(card, ctx.ref));
    });

    return () => {
      live = false;
      root.remove();
    };
  },

  picker: () => ({
    id: "orpheus-ref",
    label: "Orpheus",
    icon: "diagram-3",
  }),

  async search(q, limit) {
    let res;
    try {
      res = await fetch(
        `${API}?q=${encodeURIComponent(q)}&limit=${encodeURIComponent(limit || 10)}`,
        { headers: { accept: "application/json" }, credentials: "same-origin" }
      );
    } catch (e) {
      return [];
    }
    if (!res.ok) return [];
    const body = await res.json().catch(() => ({}));
    return (body.hits || []).map((hit) => ({
      ref: `/-/orpheus/ref/${hit.ref}`,
      label: hit.label,
      kind: hit.kind,
      detail: hit.detail,
    }));
  },
};

/**
 * The block card body. Text nodes only — nothing from the store is HTML.
 *
 * The three refusal branches are a narrow fallback, not the usual path: paper
 * calls `resolve` first for the card header, and when that comes back `denied`
 * or `not_found` it renders its own "Resource not found" chrome and never
 * calls `mount` at all — checked against a live paper. What reaches them is the
 * race where the answer changed between the two calls (a document shared or
 * unshared, a row deleted), and `error`, where `resolve` succeeded and this
 * fetch did not.
 */
function renderCard(card, ref) {
  const box = document.createElement("div");
  box.className = `orpheus-ref-card orpheus-ref-${card.status}`;

  if (card.status === "denied") {
    box.appendChild(line("p", "You do not have access to this, or it is not here."));
    box.appendChild(line("p", "The two answer the same way on purpose.", "orpheus-ref-note"));
    return box;
  }
  if (card.status === "not_found") {
    box.appendChild(line("p", "Nothing in the store answers to this reference."));
    return box;
  }
  if (card.status === "error") {
    box.appendChild(line("p", "Could not reach the store to resolve this."));
    return box;
  }
  if (card.status === "redacted") {
    box.appendChild(line("p", "This document was redacted."));
    const r = card.redaction || {};
    if (r.at) box.appendChild(line("p", `Removed ${r.at}. ${r.note || ""}`.trim(), "orpheus-ref-note"));
    return box;
  }

  // No label here: paper's own header already carries it, linked, right above
  // this. Repeating it printed every name twice on the page.
  if (card.detail) box.appendChild(line("p", card.detail, "orpheus-ref-detail"));

  const review = card.review || {};
  const state = document.createElement("p");
  state.className =
    "orpheus-ref-state " +
    (review.contested
      ? "orpheus-ref-disputed"
      : review.checked
        ? "orpheus-ref-checked"
        : "orpheus-ref-unchecked");
  state.textContent = review.checked
    ? `Checked by a person (${review.state}).`
    : review.caveat || "Nobody has checked this.";
  box.appendChild(state);

  if (card.redirect_note)
    box.appendChild(line("p", card.redirect_note, "orpheus-ref-note"));

  return box;
}

function line(tag, text, className) {
  const el = document.createElement(tag);
  el.textContent = text;
  if (className) el.className = className;
  return el;
}

export default provider;
