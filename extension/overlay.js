/**
 * Ghost presence overlay.
 *
 * Draws what each actor (bot or human) is working on over the real page: a
 * dashed ring in the actor's color around the target, the actor's mote, and a
 * label. It only shows; it never edits the page or takes input. The same file
 * is injected by the Chrome extension and can be run by the Mia in-app browser.
 *
 * Usage (after injecting this file):
 *   __ghostOverlay.show({actor_id, label, color, kind, choice | selector | text | rect})
 *   __ghostOverlay.clear(actor_id?)
 */
(() => {
  if (globalThis.__ghostOverlay) return;

  const MAX_LABEL = 80;
  const DEFAULT_TTL_MS = 10 * 60 * 1000;
  const PALETTE = ["#3b82f6", "#ef4444", "#f59e0b", "#8b5cf6", "#10b981", "#ec4899", "#0ea5e9", "#84cc16"];

  const actors = new Map(); // actor_id -> {spec, anchor, nodes, expiresAt}
  let host = null;
  let root = null;
  let frame = 0;

  const CSS = `
    :host { all: initial; }
    .layer { position: fixed; inset: 0; pointer-events: none; z-index: 2147483647; }
    .ring { position: fixed; box-sizing: border-box; border: 2px dashed var(--c);
            background: color-mix(in srgb, var(--c) 8%, transparent); border-radius: 6px;
            transition: all 120ms ease-out; }
    .ring.human { border-style: solid; background: transparent; }
    .ring.done { border-style: solid; opacity: .55; }
    .ring.failed { border-color: #dc2626; }
    .mote { position: fixed; width: 30px; height: 30px; border-radius: 50%; background: var(--c);
            box-shadow: 0 0 0 3px #fff, 0 0 0 5px var(--ring, var(--c)), 0 2px 6px rgba(0,0,0,.25);
            transition: all 120ms ease-out; }
    .mote::before, .mote::after { content: ""; position: absolute; top: 9px; width: 5px; height: 10px;
            border-radius: 3px; background: #fff; }
    .mote::before { left: 9px; } .mote::after { right: 9px; }
    .mote.working { animation: bob 1.4s ease-in-out infinite; }
    @keyframes bob { 50% { transform: translateY(-3px); } }
    .cursor { position: fixed; width: 0; height: 0; border-left: 7px solid transparent;
              border-right: 7px solid transparent; border-bottom: 18px solid var(--c);
              transform: rotate(-35deg); transform-origin: top left; }
    .label { position: fixed; font: 600 12px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
             color: #111; background: #fff; border: 1.5px solid var(--c); border-radius: 6px;
             padding: 3px 7px; white-space: nowrap; max-width: 320px; overflow: hidden;
             text-overflow: ellipsis; box-shadow: 0 1px 4px rgba(0,0,0,.15); transition: all 120ms ease-out; }
    .label.human { background: var(--c); color: #fff; border-color: var(--c); }
  `;

  function colorFor(spec) {
    if (typeof spec.color === "string" && /^#[0-9a-fA-F]{3,8}$/.test(spec.color)) return spec.color;
    let hash = 0;
    for (const ch of spec.actor_id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    return PALETTE[hash % PALETTE.length];
  }

  function ensureRoot() {
    if (host && host.isConnected) return;
    host = document.createElement("ghost-overlay");
    root = host.attachShadow({ mode: "closed" });
    // Constructable stylesheets are not blocked by page CSP the way <style> tags can be.
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    root.adoptedStyleSheets = [sheet];
    const layer = document.createElement("div");
    layer.className = "layer";
    root.appendChild(layer);
    document.documentElement.appendChild(host);
  }

  function layer() {
    ensureRoot();
    return root.querySelector(".layer");
  }

  function findText(text) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const needle = text.toLowerCase();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const index = node.textContent.toLowerCase().indexOf(needle);
      if (index < 0) continue;
      if (host && host.contains(node)) continue;
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, Math.min(node.textContent.length, index + text.length));
      return range;
    }
    return null;
  }

  // An anchor turns a target into a viewport rect on every frame, so the ring
  // follows the element while the page scrolls or re-lays out.
  function makeAnchor(spec) {
    if (Number.isInteger(spec.choice)) {
      const selector = `[data-ghost-id="${spec.choice}"]`;
      if (!document.querySelector(selector)) throw new Error(`Element ${spec.choice} not found; vacuum or read the page first`);
      return { describe: `#${spec.choice}`, rect: () => document.querySelector(selector)?.getBoundingClientRect() };
    }
    if (typeof spec.selector === "string" && spec.selector) {
      if (!document.querySelector(spec.selector)) throw new Error(`Selector not found: ${spec.selector}`);
      return { describe: spec.selector, rect: () => document.querySelector(spec.selector)?.getBoundingClientRect() };
    }
    if (typeof spec.text === "string" && spec.text) {
      let range = findText(spec.text);
      if (!range) throw new Error("Text not found on the page");
      return {
        describe: "text",
        rect: () => {
          if (!range.startContainer.isConnected) range = findText(spec.text);
          return range ? range.getBoundingClientRect() : null;
        },
      };
    }
    if (spec.rect && typeof spec.rect === "object") {
      // Page coordinates, for canvas apps where there is no element to point at.
      const { x, y, w, h } = spec.rect;
      if (![x, y, w, h].every(Number.isFinite)) throw new Error("rect needs numeric x, y, w, h");
      return { describe: "rect", rect: () => new DOMRect(x - scrollX, y - scrollY, w, h) };
    }
    return { describe: "page", rect: () => null };
  }

  function makeNodes(spec) {
    const color = colorFor(spec);
    const human = spec.kind === "human";
    const ring = document.createElement("div");
    ring.className = "ring" + (human ? " human" : "");
    const marker = document.createElement("div");
    marker.className = human ? "cursor" : "mote";
    const label = document.createElement("div");
    label.className = "label" + (human ? " human" : "");
    for (const node of [ring, marker, label]) node.style.setProperty("--c", color);
    if (!human && typeof spec.owner_color === "string" && /^#[0-9a-fA-F]{3,8}$/.test(spec.owner_color)) {
      marker.style.setProperty("--ring", spec.owner_color);
    }
    const list = layer();
    list.append(ring, marker, label);
    return { ring, marker, label };
  }

  function render(entry) {
    const { spec, anchor, nodes } = entry;
    const status = spec.status || "working";
    nodes.ring.classList.toggle("done", status === "done");
    nodes.ring.classList.toggle("failed", status === "failed");
    nodes.marker.classList.toggle("working", status === "working");
    nodes.label.textContent = spec.label ? String(spec.label).slice(0, MAX_LABEL) : spec.actor_id;

    const vw = innerWidth;
    const vh = innerHeight;
    const clampX = (v, size) => Math.max(4, Math.min(vw - size - 4, v));
    const clampY = (v, size) => Math.max(4, Math.min(vh - size - 4, v));
    const rect = anchor.rect();

    if (!rect || (!rect.width && !rect.height)) {
      // No target: park the mote in the top-right corner with its label.
      nodes.ring.style.display = "none";
      const top = 12 + 40 * entry.slot;
      Object.assign(nodes.marker.style, { left: `${vw - 46}px`, top: `${top}px` });
      nodes.label.style.left = "auto";
      nodes.label.style.right = "54px";
      nodes.label.style.top = `${top + 5}px`;
      entry.lastRect = null;
      return;
    }

    const pad = spec.kind === "human" ? 2 : 4;
    nodes.ring.style.display = "block";
    Object.assign(nodes.ring.style, {
      left: `${rect.left - pad}px`, top: `${rect.top - pad}px`,
      width: `${rect.width + pad * 2}px`, height: `${rect.height + pad * 2}px`,
    });
    if (spec.kind === "human") {
      Object.assign(nodes.marker.style, { left: `${clampX(rect.right - 12, 14)}px`, top: `${clampY(rect.bottom - 16, 18)}px` });
      Object.assign(nodes.label.style, { left: `${clampX(rect.right - 4, 60)}px`, top: `${clampY(rect.bottom, 20)}px`, right: "auto" });
    } else {
      const moteX = clampX(rect.right - 15, 30);
      const moteY = clampY(rect.top - 15, 30);
      Object.assign(nodes.marker.style, { left: `${moteX}px`, top: `${moteY}px` });
      // Put the label right of the mote, or left of it when it would run off screen.
      const labelWidth = nodes.label.offsetWidth || 80;
      const labelX = moteX + 36 + labelWidth <= vw - 4 ? moteX + 36 : moteX - 6 - labelWidth;
      Object.assign(nodes.label.style, { left: `${clampX(labelX, labelWidth)}px`, top: `${clampY(moteY + 5, 20)}px`, right: "auto" });
    }
    entry.lastRect = { x: rect.left + scrollX, y: rect.top + scrollY, w: rect.width, h: rect.height };
  }

  function tick() {
    frame = 0;
    const now = Date.now();
    for (const [id, entry] of actors) {
      if (entry.expiresAt <= now) {
        remove(id);
        continue;
      }
      render(entry);
    }
    if (actors.size) frame = requestAnimationFrame(tick);
  }

  function schedule() {
    if (!frame && actors.size) frame = requestAnimationFrame(tick);
  }

  function remove(id) {
    const entry = actors.get(id);
    if (!entry) return;
    for (const node of Object.values(entry.nodes)) node.remove();
    actors.delete(id);
  }

  // Actors without a target stack in the top-right corner.
  function reslot() {
    let slot = 0;
    for (const entry of actors.values()) entry.slot = entry.anchor.describe === "page" ? slot++ : 0;
  }

  function show(spec) {
    if (!spec || typeof spec.actor_id !== "string" || !/^[A-Za-z0-9_.:-]{1,64}$/.test(spec.actor_id)) {
      throw new Error("actor_id is required (1-64 of A-Z a-z 0-9 _ . : -)");
    }
    const anchor = makeAnchor(spec);
    remove(spec.actor_id);
    const ttl = Number.isFinite(spec.ttl_ms) ? Math.max(1000, Math.min(spec.ttl_ms, 60 * 60 * 1000)) : DEFAULT_TTL_MS;
    const entry = { spec, anchor, nodes: makeNodes(spec), expiresAt: Date.now() + ttl, slot: 0, lastRect: null };
    actors.set(spec.actor_id, entry);
    reslot();
    render(entry);
    schedule();
    return { actor_id: spec.actor_id, target: anchor.describe, rect: entry.lastRect, status: spec.status || "working" };
  }

  function clear(actorId) {
    if (actorId) remove(actorId);
    else for (const id of [...actors.keys()]) remove(id);
    reslot();
    return { cleared: actorId || "all", remaining: actors.size };
  }

  function list() {
    return [...actors.values()].map(e => ({
      actor_id: e.spec.actor_id, label: e.spec.label || "", kind: e.spec.kind || "bot",
      status: e.spec.status || "working", target: e.anchor.describe, rect: e.lastRect,
    }));
  }

  globalThis.__ghostOverlay = { show, clear, list };
})();
