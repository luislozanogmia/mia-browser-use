/**
 * Ghost presence overlay.
 *
 * Draws what each actor (bot or human) is working on over the real page: a
 * dashed ring in the actor's color around the target, the actor's mote, and a
 * label. It never edits the page. Its only input is on its own cards, and, on
 * shared pages, an Ask button next to text the human selects. The same file
 * is injected by the Chrome extension and can be run by the Mia in-app browser.
 *
 * Usage (after injecting this file):
 *   __ghostOverlay.show({actor_id, label, color, kind, choice | selector | text | rect})
 *   __ghostOverlay.clear(actor_id?)
 *   __ghostOverlay.suggest({id, kind: edit|note|ask, title, body, ...target})
 *   __ghostOverlay.enableAsk(({text, question, target}) => ...)
 */
(() => {
  const old = globalThis.__ghostOverlay;
  if (old && old.build === globalThis.__ghostBuild) return;
  // Left by an older copy of the extension: take its place.
  if (old) {
    try { old.reset?.(); } catch {}
    try { old.retire?.(); } catch {} // its timers and listeners, or they run on next to ours
    for (const node of document.querySelectorAll("ghost-overlay")) node.remove();
  }

  const MAX_LABEL = 80;
  const DEFAULT_TTL_MS = 10 * 60 * 1000;
  const PALETTE = ["#3b82f6", "#ef4444", "#f59e0b", "#8b5cf6", "#10b981", "#ec4899", "#0ea5e9", "#84cc16"];

  const actors = new Map(); // actor_id -> {spec, anchor, nodes, expiresAt}
  let host = null;
  let root = null;
  let frame = 0;

  const CSS = `
    :host { all: initial; }
    /* At the document's origin, so marks drawn in page coordinates scroll with the page. */
    .layer { position: absolute; left: 0; top: 0; width: 0; height: 0; overflow: visible;
             pointer-events: none; z-index: 2147483647; }
    .ring { position: absolute; box-sizing: border-box; border: 2px dashed var(--c);
            background: color-mix(in srgb, var(--c) 8%, transparent); border-radius: 6px;
            transition: background-color 120ms, border-color 120ms, opacity 120ms; }
    .ring.human { border-style: solid; background: transparent; }
    .ring.done { border-style: solid; opacity: .55; }
    .ring.failed { border-color: #dc2626; }
    /* The bot's Mote (Mia's clay blob, tinted to the bot's color) on a white badge ringed in its owner's color. */
    .mote { position: absolute; width: 30px; height: 30px; border-radius: 50%; background: #fff;
            box-shadow: 0 0 0 2px #fff, 0 0 0 4px var(--ring, var(--c)), 0 2px 6px rgba(0,0,0,.25);
            transition: opacity 120ms; }
    .mote::before { content: ""; position: absolute; inset: 1px; background: var(--mote-img) center / contain no-repeat;
            filter: hue-rotate(var(--hue, 0deg)); }
    .mote.working { animation: bob 1.4s ease-in-out infinite; }
    @keyframes bob { 50% { transform: translateY(-3px); } }
    .cursor { position: absolute; width: 0; height: 0; border-left: 7px solid transparent;
              border-right: 7px solid transparent; border-bottom: 18px solid var(--c);
              transform: rotate(-35deg); transform-origin: top left; }
    .label { position: absolute; font: 600 12px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
             color: #111; background: #fff; border: 1.5px solid var(--c); border-radius: 6px;
             padding: 3px 7px; white-space: nowrap; max-width: 320px; overflow: hidden;
             text-overflow: ellipsis; box-shadow: 0 1px 4px rgba(0,0,0,.15); transition: background-color 120ms, border-color 120ms, opacity 120ms; }
    .label.human { background: var(--c); color: #fff; border-color: var(--c); }
    .card { position: absolute; width: 260px; box-sizing: border-box; pointer-events: auto;
            font: 13px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #111;
            background: #fff; border: 2px solid var(--c); border-radius: 10px; padding: 10px 12px;
            box-shadow: 0 4px 14px rgba(0,0,0,.18); }
    .card h4 { margin: 0 0 4px; font-size: 14px; }
    .card p { margin: 0 0 8px; color: #333; }
    .card .who { font-weight: 600; }
    .card button { font: inherit; border: 1px solid #ccc; background: #f7f7f7; border-radius: 6px;
                   padding: 3px 10px; margin-right: 6px; cursor: pointer; }
    .card button:hover { background: #eee; }
    .card .state { font-weight: 600; }
    .card .close { position: absolute; top: 6px; right: 6px; margin: 0; padding: 0 6px; border: 0;
                   background: transparent; color: #666; font-size: 16px; line-height: 20px; }
    .card .quote { margin: 0 0 6px; color: #555; font-size: 12px; font-style: italic; }
    .card.ask { border-style: dashed; }
    .card .waiting { color: #666; font-size: 12px; margin: 0; display: flex; align-items: center; gap: 8px; }
    .card .waiting .stop { margin: 0; padding: 1px 8px; border: 1px solid #ccc; border-radius: 6px; background: #fff;
      color: #333; font: inherit; font-size: 11px; cursor: pointer; }
    .card .waiting .stop:hover { background: #f3f3f3; }
    .card h4 { padding-right: 18px; }
    .ask-button { position: absolute; pointer-events: auto; font: 600 12px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                  color: #fff; background: #111; border: 0; border-radius: 14px; padding: 7px 11px; cursor: pointer;
                  box-shadow: 0 2px 8px rgba(0,0,0,.25); }
    .ask-box { position: absolute; width: 280px; box-sizing: border-box; pointer-events: auto; background: #fff;
               border: 2px solid #111; border-radius: 10px; padding: 8px; box-shadow: 0 4px 14px rgba(0,0,0,.18);
               font: 13px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #111; }
    .ask-box textarea { width: 100%; box-sizing: border-box; font: inherit; border: 1px solid #ccc; border-radius: 6px;
                        padding: 6px; resize: none; }
    .ask-box .hint { color: #666; font-size: 11px; margin-top: 4px; }
    .ask-box img { display: block; max-width: 100%; max-height: 90px; margin: 0 0 6px; border-radius: 4px;
                   border: 1px solid #ddd; object-fit: contain; }
    .crop { position: fixed; inset: 0; pointer-events: auto; cursor: crosshair; background: rgba(0,0,0,.12); z-index: 1; }
    .crop.sticky { background: transparent; pointer-events: none; }
    .card, .ask-box, .ask-button, .toast, .crop-box { z-index: 2; }
    .card.conv { width: 300px; }
    .card, .ask-box { max-width: calc(100vw - 8px); }
    .conv .turns { max-height: 340px; overflow-y: auto; margin: 0 0 6px; }
    .conv .turn { margin: 0 0 8px; }
    .conv .turn .quote { margin: 0 0 4px; }
    .conv .turn .a-title { font-weight: 600; margin: 0 0 2px; }
    .conv .turn .a-body { margin: 0 0 6px; }
    .card ul { margin: 0 0 8px; padding-left: 18px; color: #333; }
    .card li { margin: 0 0 3px; }
    .card code { font: 12px/1.4 ui-monospace, Menlo, monospace; background: #f1f3f5; padding: 0 4px; border-radius: 4px; }
    .conv textarea { width: 100%; box-sizing: border-box; font: inherit; border: 1px solid #ccc; border-radius: 6px;
                     padding: 5px 6px; resize: none; }
    .crop-hint { position: fixed; left: 50%; top: 14px; transform: translateX(-50%); pointer-events: none;
                 font: 600 12px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #fff;
                 background: #111; border-radius: 14px; padding: 8px 12px; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
    .crop-box { position: absolute; box-sizing: border-box; border: 2px solid #111; border-radius: 4px;
                background: rgba(255,255,255,.08); box-shadow: 0 0 0 1px #fff; pointer-events: none; }
    .toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); pointer-events: none;
             font: 13px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #fff;
             background: #111; border-radius: 8px; padding: 9px 14px; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
  `;

  function colorFor(spec) {
    if (typeof spec.color === "string" && /^#[0-9a-fA-F]{3,8}$/.test(spec.color)) return spec.color;
    let hash = 0;
    for (const ch of spec.actor_id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    return PALETTE[hash % PALETTE.length];
  }

  // The Mote art is pink (hue 337); turn it to the bot's own color.
  function moteHue(hex) {
    let h = hex.slice(1);
    if (h.length < 6) h = [...h.slice(0, 3)].map(c => c + c).join("");
    const [r, g, b] = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
    const max = Math.max(r, g, b), d = max - Math.min(r, g, b);
    if (!d) return 0;
    const hue = max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return Math.round(((hue * 60 - 337 + 540) % 360) - 180);
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

  // Place a node at viewport point (x, y). Marks on the page are stored in page
  // coordinates so the browser scrolls them with the content, with no lag;
  // pinned ones (parked in a corner) stay put in the window.
  function put(node, x, y, pinned = false) {
    node.style.position = pinned ? "fixed" : "absolute";
    node.style.left = `${pinned ? x : x + scrollX}px`;
    node.style.top = `${pinned ? y : y + scrollY}px`;
    node.style.right = "auto";
  }

  const NOT_SHOWN = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "TEXTAREA"]);

  // Only text a person can see: apps like Google Docs keep the document's
  // words in <script> data too, and that must not win over what is drawn.
  // Matching ignores case and whitespace and runs across element boundaries,
  // so a selection that spans links or bold text is found again after the
  // page reflows (a resize, a font change).
  function findText(text, end = "") {
    const needle = text.toLowerCase().replace(/\s+/g, "");
    const tail = end.toLowerCase().replace(/\s+/g, "");
    if (!needle) return null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: node => (NOT_SHOWN.has(node.parentElement?.tagName) || (host && host.contains(node))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    let joined = "";
    const where = []; // joined index -> [node, offset]
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const value = node.textContent;
      for (let i = 0; i < value.length; i++) {
        if (/\s/.test(value[i])) continue;
        joined += value[i].toLowerCase();
        where.push([node, i]);
      }
    }
    for (let from = joined.indexOf(needle); from >= 0; from = joined.indexOf(needle, from + 1)) {
      let last = from + needle.length - 1;
      if (tail) {
        // A long selection: run to where its end text appears, close after the start.
        const at = joined.indexOf(tail, from);
        if (at < 0 || at - from > 20000) continue;
        last = Math.max(last, at + tail.length - 1);
      }
      const [startNode, startOffset] = where[from];
      const [endNode, endOffset] = where[last];
      const range = document.createRange();
      range.setStart(startNode, startOffset);
      range.setEnd(endNode, endOffset + 1);
      const box = range.getBoundingClientRect();
      if (box.width || box.height) return range;
    }
    return null;
  }

  function elementAnchor(el, describe) {
    return { describe, element: el, rect: () => (el.isConnected ? el.getBoundingClientRect() : null) };
  }

  // Canvas apps (Google Docs) have no text nodes; canvas_text.js, running in
  // the page, knows where each drawn string is.
  function canvasRect(text) {
    const html = document.documentElement;
    html.removeAttribute("data-ghost-canvas-hit");
    document.dispatchEvent(new CustomEvent("ghost-canvas-find", { detail: text }));
    const raw = html.getAttribute("data-ghost-canvas-hit");
    if (!raw) return null;
    try {
      const { x, y, w, h } = JSON.parse(raw);
      return [x, y, w, h].every(Number.isFinite) ? new DOMRect(x, y, w, h) : null;
    } catch {
      return null;
    }
  }

  function textAnchor(text, describe = "text", end = "") {
    let range = findText(text, end);
    if (!range) {
      if (!canvasRect(text)) return null;
      return { describe: `${describe} (canvas)`, rect: () => canvasRect(text) };
    }
    let retryAt = 0;
    return {
      describe,
      rect: () => {
        if ((!range || !range.startContainer.isConnected) && Date.now() >= retryAt) {
          range = findText(text, end);
          retryAt = Date.now() + 1000; // searching the page every frame would be costly
        }
        return range && range.startContainer.isConnected ? range.getBoundingClientRect() : null;
      },
    };
  }

  // An area inside an element, in the element's content coordinates: it moves
  // with the element, and with the element's own scrolling when the element is
  // the scrolling panel itself (a feed that scrolls inside the page).
  function areaAnchor(el, offset, describe) {
    const { x, y, w, h } = offset;
    return {
      describe, element: el,
      rect: () => {
        if (!el.isConnected) return null;
        const r = el.getBoundingClientRect();
        return new DOMRect(r.left + x - el.scrollLeft, r.top + y - el.scrollTop, w, h);
      },
    };
  }

  function validOffset(offset) {
    return offset && typeof offset === "object" && ["x", "y", "w", "h"].every(k => Number.isFinite(offset[k]));
  }

  function rectAnchor(rect) {
    const { x, y, w, h } = rect;
    if (![x, y, w, h].every(Number.isFinite)) throw new Error("rect needs numeric x, y, w, h");
    return { describe: "rect", rect: () => new DOMRect(x - scrollX, y - scrollY, w, h) };
  }

  // An anchor turns a target into a viewport rect on every frame, so the ring
  // follows the element while the page scrolls or re-lays out.
  function makeAnchor(spec) {
    if (spec.element instanceof Element) return elementAnchor(spec.element, "element");
    if (Number.isInteger(spec.choice)) {
      // The actor's own numbers, from its last read or vacuum of this page.
      return elementAnchor(globalThis.__ghostPage.resolve(spec.actor_id, spec.choice), `#${spec.choice}`);
    }
    if (typeof spec.selector === "string" && spec.selector) {
      // Inside shadow roots and frames too (LinkedIn's chat window), like the action itself found it.
      const el = globalThis.__ghostPage?.deepQuery ? globalThis.__ghostPage.deepQuery(spec.selector) : document.querySelector(spec.selector);
      if (!el) throw new Error(`Selector not found: ${spec.selector}`);
      return elementAnchor(el, spec.selector);
    }
    if (typeof spec.text === "string" && spec.text) {
      const anchor = textAnchor(spec.text);
      if (!anchor) throw new Error("Text not found on the page");
      return anchor;
    }
    if (spec.rect && typeof spec.rect === "object") {
      // Page coordinates, for canvas apps where there is no element to point at.
      return rectAnchor(spec.rect);
    }
    if (spec.anchor && typeof spec.anchor === "object") {
      // A portable anchor published from another browser: try the most exact form first.
      const { selector, text, end, rect, offset } = spec.anchor;
      const el = typeof selector === "string" && selector ? safeQuery(selector) : null;
      if (el && validOffset(offset)) return areaAnchor(el, offset, "area");
      if (el) return elementAnchor(el, "anchor");
      const byText = typeof text === "string" && text ? textAnchor(text.slice(0, 500), "anchor", typeof end === "string" ? end.slice(0, 200) : "") : null;
      if (byText) return byText;
      if (rect && typeof rect === "object") return rectAnchor(rect);
    }
    return { describe: "page", rect: () => null };
  }

  function safeQuery(selector) {
    try { return document.querySelector(selector); } catch { return null; }
  }

  /** The portable form of where an actor is, for other people's browsers. */
  function portable(spec, anchor, lastRect) {
    if (anchor.describe === "area" && spec.anchor) return spec.anchor; // keep the exact area, not the whole element
    if (anchor.element && globalThis.__ghostPage) return globalThis.__ghostPage.anchorOf(anchor.element);
    if (typeof spec.text === "string" && spec.text) return { text: spec.text, rect: lastRect };
    if (spec.anchor) return spec.anchor;
    return lastRect ? { rect: lastRect } : null;
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
    if (!human) {
      if (globalThis.__ghostMote) marker.style.setProperty("--mote-img", `url("${globalThis.__ghostMote}")`);
      marker.style.setProperty("--hue", `${moteHue(color)}deg`);
    }
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
    // Only sideways: clamping up or down would pin a mark to the window edge
    // and make it ride along while the page scrolls.
    const clampX = (v, size) => Math.max(4, Math.min(vw - size - 4, v));
    let rect = anchor.rect();
    if ((!rect || (!rect.width && !rect.height)) && spec.kind === "human" && entry.pointerAnchor) {
      // Pointer only: draw the cursor without a ring.
      const pr = entry.pointerAnchor.rect();
      if (pr) {
        nodes.ring.style.display = "none";
        const px = pr.left + pr.width * entry.pointer.fx;
        const py = pr.top + pr.height * entry.pointer.fy;
        put(nodes.marker, clampX(px, 14), py);
        put(nodes.label, clampX(px + 10, 60), py + 16);
        entry.lastRect = null;
        return;
      }
    }

    if (!rect || (!rect.width && !rect.height)) {
      // No target: park the mote in the top-right corner with its label.
      nodes.ring.style.display = "none";
      const top = 12 + 40 * entry.slot;
      put(nodes.marker, vw - 46, top, true);
      put(nodes.label, 0, top + 5, true);
      nodes.label.style.left = "auto";
      nodes.label.style.right = "54px";
      entry.lastRect = null;
      return;
    }

    const pad = spec.kind === "human" ? 2 : 4;
    // A bot reading the area a question already boxes keeps its face and name but not a second box.
    nodes.ring.style.display = spec.kind !== "human" && boxedByCard(rect) ? "none" : "block";
    put(nodes.ring, rect.left - pad, rect.top - pad);
    Object.assign(nodes.ring.style, { width: `${rect.width + pad * 2}px`, height: `${rect.height + pad * 2}px` });
    if (spec.kind === "human") {
      // A live pointer wins over the corner of the focused element.
      const pr = entry.pointerAnchor ? entry.pointerAnchor.rect() : null;
      const px = pr ? pr.left + pr.width * entry.pointer.fx : rect.right - 12;
      const py = pr ? pr.top + pr.height * entry.pointer.fy : rect.bottom - 16;
      put(nodes.marker, clampX(px, 14), py);
      put(nodes.label, clampX(px + 10, 60), py + 16);
    } else {
      // Beside the target, not on it: right of the ring, else left of it,
      // else over its top-right corner when it spans the whole width.
      const line = Math.min(rect.height, 30);
      let moteX = rect.right + pad + 6;
      let moteY = rect.top + line / 2 - 15;
      if (moteX + 30 > vw - 4) moteX = rect.left - pad - 36;
      if (moteX < 4) {
        moteX = rect.right - 15;
        moteY = rect.top - 34;
      }
      moteX = clampX(moteX, 30);
      put(nodes.marker, moteX, moteY);
      // Put the label right of the mote, or left of it when it would run off screen.
      const labelWidth = nodes.label.offsetWidth || 80;
      const labelX = moteX + 36 + labelWidth <= vw - 4 ? moteX + 36 : moteX - 6 - labelWidth;
      put(nodes.label, clampX(labelX, labelWidth), moteY + 5);
    }
    entry.lastRect = { x: rect.left + scrollX, y: rect.top + scrollY, w: rect.width, h: rect.height };
  }

  // Most of rect lies inside an open card's box.
  function boxedByCard(rect) {
    const area = rect.width * rect.height;
    if (!area) return false;
    for (const card of cards.values()) {
      if (card.away) continue;
      const r = card.anchor.rect();
      if (!r) continue;
      const w = Math.min(rect.right, r.right + 4) - Math.max(rect.left, r.left - 4);
      const h = Math.min(rect.bottom, r.bottom + 4) - Math.max(rect.top, r.top - 4);
      if (w > 0 && h > 0 && w * h >= area * 0.6) return true;
    }
    return false;
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
    placeCards();
    if (actors.size || cards.size) frame = requestAnimationFrame(tick);
  }

  function schedule() {
    if (!frame && (actors.size || cards.size)) frame = requestAnimationFrame(tick);
  }

  // -- Suggestions: a bot proposes, a human accepts or rejects --------------

  // "luis's Upwork bot" for a bot named after its site, else "Ledger · luis's bot".
  function botTitle(name, owner) {
    if (!owner) return name;
    return /\bbot( \d+)?$/i.test(name) ? `${owner}'s ${name}` : `${name} · ${owner}'s bot`;
  }

  const cards = new Map(); // suggestion id -> {spec, anchor, node, ring}

  const CARD_WIDTH = 260;
  const CARD_GAP = 8;

  function overlaps(a, b) {
    return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  }

  // The right edge of the text block a target sits in (its paragraph, or the
  // page canvas in Google Docs), so a card never covers the rest of the line.
  function blockRight(rect, vw) {
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    if (!hit || hit === host || hit === document.body || hit === document.documentElement) return rect.right;
    const box = hit.getBoundingClientRect();
    return box.width < vw * 0.8 ? Math.max(rect.right, box.right) : rect.right;
  }

  // Lay cards out like comments in a document margin: in one column right of
  // every target when there is room, else under their own target. Either way
  // a card slides down past other cards, other targets and bots' labels, so
  // nothing covers anything else.
  function placeCards() {
    // The page's width without its scrollbar; cards stay inside it.
    const vw = document.documentElement.clientWidth || innerWidth;
    const placed = [];
    const obstacles = [];
    for (const entry of actors.values()) {
      for (const node of [entry.nodes.marker, entry.nodes.label]) {
        const r = node.getBoundingClientRect();
        if (r.width) obstacles.push(r);
      }
    }
    const items = [...cards.values()].filter(card => !card.away).map(card => ({ card, rect: card.anchor.rect() }));
    for (const item of items) {
      const { rect, card } = item;
      if (rect && (rect.width || rect.height)) {
        put(card.ring, rect.left - 4, rect.top - 4);
        Object.assign(card.ring.style, { display: "block", width: `${rect.width + 8}px`, height: `${rect.height + 8}px` });
        obstacles.push({ left: rect.left - 4, right: rect.right + 4, top: rect.top - 4, bottom: rect.bottom + 4, card });
      } else {
        card.ring.style.display = "none";
        item.rect = null;
      }
    }
    const targeted = items.filter(i => i.rect);
    const margin = Math.max(0, ...targeted.map(i => blockRight(i.rect, vw)), ...obstacles.filter(o => !o.card).map(o => o.right)) + 16;
    const widest = Math.max(CARD_WIDTH, ...items.map(i => i.card.node.offsetWidth || 0));
    const column = targeted.length && margin + widest <= vw - 4 ? margin : null;
    items.sort((a, b) => (a.rect ? a.rect.top : Infinity) - (b.rect ? b.rect.top : Infinity));
    let parked = 60;
    for (const { card, rect } of items) {
      const height = card.node.offsetHeight || 120;
      const width = card.node.offsetWidth || CARD_WIDTH;
      let left;
      let top;
      if (!rect) {
        left = vw - width - 16;
        top = parked;
      } else if (column !== null) {
        left = column;
        top = rect.top;
      } else {
        left = Math.max(4, Math.min(vw - width - 4, rect.left));
        top = rect.bottom + 10;
      }
      if (!rect) top = Math.max(4, top);
      left = Math.max(4, Math.min(vw - width - 4, left));  // never past either edge
      const box = () => ({ left, right: left + width, top, bottom: top + height });
      // Slide past other cards and targets: down, or up when the card opens upward.
      const slide = up => {
        for (let moved = true, tries = 0; moved && tries < 50; tries++) {
          moved = false;
          for (const o of [...placed, ...obstacles]) {
            if (o.card === card || !overlaps(box(), o)) continue;
            top = up ? o.top - CARD_GAP - height : o.bottom + CARD_GAP;
            moved = true;
          }
        }
      };
      slide(false);
      // No room below a target on screen (a call's control bar, a footer): open upward instead.
      if (rect && rect.top < innerHeight && top + height > innerHeight - 4) {
        top = column !== null ? rect.bottom - height : rect.top - 10 - height;
        top = Math.min(top, innerHeight - 4 - height);
        slide(true);
        if (top < 4) top = Math.max(4, innerHeight - 4 - height); // taller than the room above: keep it in the window
      }
      if (!rect) parked = top + height + CARD_GAP;
      placed.push(box());
      put(card.node, left, top, !rect);
    }
  }

  const KINDS = new Set(["edit", "note", "ask"]);

  function suggest(spec) {
    if (!spec || typeof spec.id !== "string" || !/^[A-Za-z0-9_.:-]{1,64}$/.test(spec.id)) throw new Error("suggestion id is required");
    const kind = KINDS.has(spec.kind) ? spec.kind : "edit";
    const given = spec.actor && typeof spec.actor === "object" ? spec.actor : {};
    // Look like the actor already on the page: its label's name and its color.
    const shown = actors.get(given.id || spec.actor_id)?.spec || {};
    const shownName = typeof shown.label === "string" ? shown.label.split("·")[0].trim() : "";
    const actor = {
      ...given,
      name: given.name && given.name !== given.id ? given.name : shownName || given.name,
      color: given.color || shown.color,
    };
    // A question and its answers (and follow-ups) are one conversation card.
    const thread = typeof spec.thread === "string" && spec.thread ? spec.thread
      : kind === "ask" ? spec.id : typeof spec.reply_to === "string" ? spec.reply_to : "";
    if (thread && (kind === "ask" || (kind === "note" && spec.reply_to))) return converse(spec, kind, thread, actor);
    unsuggest(spec.id);
    // An answer takes the place of the question it answers.
    if (typeof spec.reply_to === "string") unsuggest(spec.reply_to);
    const anchor = makeAnchor({ ...spec, actor_id: actor.id || spec.actor_id });
    const color = colorFor({ actor_id: actor.id || "bot", color: actor.color });
    const ring = document.createElement("div");
    ring.className = "ring";
    ring.style.setProperty("--c", color);
    const node = document.createElement("div");
    node.className = `card ${kind}`;
    node.style.setProperty("--c", color);
    const title = document.createElement("h4");
    const who = document.createElement("span");
    who.className = "who";
    const name = actor.name || actor.id || "Bot";
    who.textContent = kind === "ask" ? `${name} asked` : botTitle(name, actor.owner);
    title.append(who);
    const parts = [title];
    if (spec.question) {
      const quote = document.createElement("p");
      quote.className = "quote";
      quote.textContent = `“${String(spec.question).slice(0, 200)}”`;
      parts.push(quote);
    }
    const heading = document.createElement("p");
    heading.textContent = String(spec.title || "").slice(0, kind === "ask" ? 600 : 120);
    heading.style.fontWeight = "600";
    parts.push(heading);
    if (spec.body) {
      parts.push(...markdownBlocks(String(spec.body).slice(0, 2000), ""));
    }
    if (kind === "edit") {
      // Only a proposed change needs a decision.
      const buttons = document.createElement("div");
      for (const decision of ["accept", "reject"]) {
        const button = document.createElement("button");
        button.textContent = decision === "accept" ? "Accept" : "Reject";
        button.addEventListener("click", () => {
          buttons.replaceChildren(Object.assign(document.createElement("span"), { className: "state", textContent: decision === "accept" ? "Accepted" : "Rejected" }));
          if (typeof api.onResolve === "function") api.onResolve(spec.id, decision);
        });
        buttons.append(button);
      }
      parts.push(buttons);
    } else {
      if (kind === "ask") parts.push(Object.assign(document.createElement("p"), { className: "waiting", textContent: "Waiting for a bot to answer…" }));
      // Notes and questions close for this person only.
      const close = Object.assign(document.createElement("button"), { className: "close", textContent: "×", title: "Close" });
      close.addEventListener("click", () => unsuggest(spec.id));
      parts.push(close);
    }
    node.append(...parts);
    layer().append(ring, node);
    const card = { spec, anchor, node, ring };
    cards.set(spec.id, card);
    showIfHere(card);
    placeCards();
    schedule();
    return { id: spec.id, kind, target: anchor.describe, anchor: portable(spec, anchor, null) };
  }

  // -- Same page, another address: single-page apps (a YouTube search) change
  // the address without loading a new page. A question and its answer belong
  // to the address they were asked on, and come back with it (Back).

  const here = () => location.href.split("#")[0];

  // A bot's answer is short markdown: paragraphs, "- " bullets, **bold** and `code`. Built as nodes, never as HTML.
  function inlineMarkdown(text, into) {
    const re = /\*\*([^*]+)\*\*|`([^`]+)`/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) into.append(text.slice(last, m.index));
      into.append(Object.assign(document.createElement(m[1] ? "strong" : "code"), { textContent: m[1] || m[2] }));
      last = re.lastIndex;
    }
    if (last < text.length) into.append(text.slice(last));
  }

  function markdownBlocks(text, className) {
    const blocks = [];
    let list = null;
    for (const raw of String(text).split("\n")) {
      const line = raw.trim();
      if (!line) { list = null; continue; }
      const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
      if (bullet) {
        if (!list) blocks.push(list = Object.assign(document.createElement("ul"), { className }));
        const item = document.createElement("li");
        inlineMarkdown(bullet[1], item);
        list.append(item);
      } else {
        list = null;
        const p = Object.assign(document.createElement("p"), { className });
        inlineMarkdown(line.replace(/^#{1,6}\s+/, ""), p);
        blocks.push(p);
      }
    }
    return blocks;
  }

  function showIfHere(card) {
    const href = typeof card.spec.href === "string" ? card.spec.href.split("#")[0] : "";
    card.away = Boolean(href) && href !== here();
    card.node.style.display = card.away ? "none" : "";
    if (card.away) card.ring.style.display = "none";
  }

  let lastHref = here();
  const hrefTimer = setInterval(() => {
    if (here() === lastHref) return;
    lastHref = here();
    hideAsk(); // a crop in progress was about the old address
    for (const card of cards.values()) showIfHere(card);
    placeCards();
  }, 400);

  // -- Conversations: ask, get an answer, reply, get another ------------------

  const convs = new Map(); // thread -> card
  const DEFAULT_QUESTION = "Explain this.";

  function converse(spec, kind, thread, actor) {
    const id = `c:${thread}`;
    let card = convs.get(thread);
    if (!card || !cards.has(id)) {
      const anchor = makeAnchor({ ...spec, actor_id: actor.id || spec.actor_id });
      const color = colorFor({ actor_id: actor.id || "bot", color: actor.color });
      const ring = Object.assign(document.createElement("div"), { className: "ring" });
      ring.style.setProperty("--c", color);
      const node = Object.assign(document.createElement("div"), { className: "card note conv" });
      node.style.setProperty("--c", color);
      const who = Object.assign(document.createElement("span"), { className: "who" });
      const heading = document.createElement("h4");
      heading.append(who);
      const turnsBox = Object.assign(document.createElement("div"), { className: "turns" });
      const close = Object.assign(document.createElement("button"), { className: "close", textContent: "×", title: "Close (saved to your reel)" });
      node.append(heading, turnsBox, close);
      layer().append(ring, node);
      card = { spec: { ...spec }, anchor, node, ring, who, turnsBox, close, turns: [], thread, conv: true, text: "" };
      close.addEventListener("click", () => closeConversation(card));
      convs.set(thread, card);
      cards.set(id, card);
    }
    addReply(card);
    if (spec.href) card.spec.href = spec.href;
    if (spec.anchor && !card.spec.anchor) card.spec.anchor = spec.anchor;
    if (!card.text && typeof spec.text === "string") card.text = spec.text;
    const askId = kind === "ask" ? spec.id : spec.reply_to;
    let turn = card.turns.find(t => t.ask === askId);
    if (!turn) {
      turn = { ask: askId, q: "", by: "", a: null };
      card.turns.push(turn);
    }
    const name = actor.name || actor.id || "Bot";
    if (kind === "ask") {
      turn.q = String(spec.title || "").slice(0, 600);
      turn.by = name;
    } else {
      turn.q ||= String(spec.question || "").slice(0, 600);
      turn.a = { title: String(spec.title || "").slice(0, 120), body: String(spec.body || "").slice(0, 2000),
                 by: botTitle(name, actor.owner) };
    }
    renderConversation(card);
    showIfHere(card);
    placeCards();
    schedule();
    return { id: spec.id, kind, target: card.anchor.describe, anchor: portable(spec, card.anchor, null) };
  }

  // The Reply box, once asking is enabled. A card drawn before this page's
  // tracker was injected (the bridge redraws as soon as a page is shared) gets it then.
  function addReply(card) {
    if (card.reply || !askHandler) return;
    const reply = document.createElement("textarea");
    reply.rows = 1;
    reply.maxLength = 600;
    reply.placeholder = "Reply…";
    for (const type of ["keydown", "keyup", "keypress", "input"]) reply.addEventListener(type, e => e.stopPropagation());
    reply.addEventListener("keydown", event => {
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      const question = reply.value.trim();
      if (!question || !askHandler) return;
      reply.value = "";
      // The room fills in the selection and the earlier turns from the thread's first question.
      try {
        askHandler({ text: card.text || "", question: question.slice(0, 600), target: card.spec.anchor || null, image: null, thread: card.thread });
      } catch {}
    });
    card.reply = reply;
    card.close.before(reply);
  }

  function renderConversation(card) {
    const answered = [...card.turns].reverse().find(t => t.a);
    card.who.textContent = answered ? answered.a.by : `${card.turns[0]?.by || "Someone"} asked`;
    card.node.classList.toggle("ask", !answered);
    const parts = [];
    for (const turn of card.turns) {
      const box = Object.assign(document.createElement("div"), { className: "turn" });
      box.append(Object.assign(document.createElement("p"), { className: "quote", textContent: `${turn.by ? `${turn.by}: ` : ""}“${turn.q}”` }));
      if (turn.a) {
        box.append(Object.assign(document.createElement("p"), { className: "a-title", textContent: turn.a.title }));
        if (turn.a.body) box.append(...markdownBlocks(turn.a.body, "a-body"));
      } else {
        const waiting = Object.assign(document.createElement("p"), { className: "waiting",
          textContent: turn.stopping ? "Stopping…" : "Waiting for a bot to answer…" });
        if (!turn.stopping && typeof api.onCancel === "function") {
          const stop = Object.assign(document.createElement("button"), { className: "stop", textContent: "Stop", title: "Stop the bot working on this" });
          stop.addEventListener("click", () => {
            turn.stopping = true;
            try { api.onCancel(turn.ask); } catch {}
            renderConversation(card);
          });
          waiting.append(stop);
        }
        box.append(waiting);
      }
      parts.push(box);
    }
    card.turnsBox.replaceChildren(...parts);
    card.turnsBox.scrollTop = card.turnsBox.scrollHeight;
  }

  async function closeConversation(card) {
    // Closing a question that is still waiting stops the bot working on it.
    for (const turn of card.turns) {
      if (!turn.a && !turn.stopping && typeof api.onCancel === "function") {
        try { api.onCancel(turn.ask); } catch {}
      }
    }
    const conversation = {
      thread: card.thread, href: card.spec.href || location.href, title: document.title,
      turns: card.turns.map(t => ({ q: t.q, by: t.by, a: t.a })),
    };
    // Saved (with a picture of the card) before it goes away.
    if (typeof api.onClose === "function") {
      await Promise.race([Promise.resolve(api.onClose(conversation)).catch(() => {}), new Promise(r => setTimeout(r, 2000))]);
    }
    cards.delete(`c:${card.thread}`);
    convs.delete(card.thread);
    card.node.remove();
    card.ring.remove();
    placeCards();
  }

  function unsuggest(id) {
    const card = cards.get(id);
    if (!card) return false;
    card.node.remove();
    card.ring.remove();
    cards.delete(id);
    return true;
  }

  // -- Ask: the human selects text and asks the bots about it ----------------

  let askHandler = null;
  let askListening = false;
  let askButton = null;
  let askBox = null;
  let askTimer = 0;
  let captureHandler = null; // (viewport rect) => Promise<jpeg data URL>, from the extension
  let cropLayer = null;
  let cropBox = null; // the area being asked about, shown until the question is sent

  function hideAsk(keepBox = false) {
    askButton?.remove();
    askButton = null;
    if (!keepBox) {
      askBox?.remove();
      askBox = null;
      cropBox?.remove();
      cropBox = null;
      stopFollow?.();
    }
  }

  function toast(message, ms = 2600) {
    const node = Object.assign(document.createElement("div"), { className: "toast", textContent: String(message).slice(0, 200) });
    layer().append(node);
    setTimeout(() => node.remove(), ms);
  }

  // Visible text inside a viewport rect, for bots that cannot see the image.
  function textIn(rect) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: node => (NOT_SHOWN.has(node.parentElement?.tagName) || (host && host.contains(node))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const parts = [];
    let size = 0;
    const range = document.createRange();
    for (let node = walker.nextNode(); node && size < 4000; node = walker.nextNode()) {
      if (!node.textContent.trim()) continue;
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (!r.width || r.right < rect.left || r.left > rect.right || r.bottom < rect.top || r.top > rect.bottom) continue;
      const text = node.textContent.replace(/\s+/g, " ").trim();
      parts.push(text);
      size += text.length + 1;
    }
    return parts.join(" ").slice(0, 4000);
  }

  // The page the area is on: the panel that scrolls it (a feed that scrolls
  // inside the page), so the area keeps its place on the page, not on a post.
  // When the window itself scrolls, page coordinates already do that.
  function areaHolder(rect) {
    const cssPath = globalThis.__ghostPage?.cssPath;
    if (!cssPath) return null;
    let el = document.elementFromPoint((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2);
    for (; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      if (host && (el === host || host.contains(el))) return null;
      const style = getComputedStyle(el);
      const scrolls = (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight)
        || (/(auto|scroll)/.test(style.overflowX) && el.scrollWidth > el.clientWidth);
      if (!scrolls) continue;
      const r = el.getBoundingClientRect();
      return { el, selector: cssPath(el), offset: {
        x: rect.left - r.left + el.scrollLeft, y: rect.top - r.top + el.scrollTop, w: rect.width, h: rect.height } };
    }
    return null;
  }

  // Where a pinned area is in the window now, as a rect with right and bottom.
  function viewRect(anchor) {
    const r = anchor.rect();
    return r ? { left: r.left, top: r.top, right: r.left + r.width, bottom: r.top + r.height, width: r.width, height: r.height } : null;
  }

  // Keep the cropped area's box on its content while any container scrolls.
  let stopFollow = null;
  // Other nodes (the question box) move by as much as the area does.
  function followBox(box, anchor, others = []) {
    stopFollow?.();
    let pending = 0;
    const first = anchor.rect();
    const starts = others.map(node => ({ node, x: parseFloat(node.style.left) - scrollX, y: parseFloat(node.style.top) - scrollY }));
    const place = () => {
      pending = 0;
      const r = anchor.rect();
      if (!r) return;
      put(box, r.left, r.top);
      Object.assign(box.style, { width: `${r.width}px`, height: `${r.height}px` });
      if (first) for (const s of starts) put(s.node, s.x + r.left - first.left, s.y + r.top - first.top);
    };
    const onScroll = () => { if (!pending) pending = requestAnimationFrame(place); };
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    addEventListener("resize", onScroll, { passive: true });
    stopFollow = () => {
      cancelAnimationFrame(pending);
      document.removeEventListener("scroll", onScroll, { capture: true });
      removeEventListener("resize", onScroll);
      stopFollow = null;
    };
  }

  // Immersive: crop again and again without the shortcut. Skip: no question box.
  const modes = { immersive: false, skip: false };
  let immersivePaused = false;

  // Called again whenever the extension re-applies the settings (a page shared,
  // a tab reloaded), so only a change does anything: a pause or a question
  // being typed must survive the same settings arriving again.
  function setModes(next = {}) {
    const immersive = Boolean(next.immersive);
    const skip = Boolean(next.skip);
    if (immersive !== modes.immersive) immersivePaused = false;
    if (skip && !modes.skip) hideAsk();
    modes.immersive = immersive;
    modes.skip = skip;
    if (modes.immersive && !immersivePaused && askHandler && !cropLayer) startCrop(true);
    if (!modes.immersive && cropLayer?.classList.contains("sticky")) {
      cropLayer.dispatchEvent(new CustomEvent("ghost-stop"));
    }
    return { ...modes };
  }

  // The element under a point, as if Ghost's crop layer weren't there.
  function underLayer(x, y) {
    if (!cropLayer) return document.elementFromPoint(x, y);
    cropLayer.style.pointerEvents = "none";
    const el = document.elementFromPoint(x, y);
    cropLayer.style.pointerEvents = "";
    return el;
  }

  function scrollerOf(el) {
    for (; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      const style = getComputedStyle(el);
      if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight) return el;
    }
    return null;
  }

  // In immersive mode the pointer carries a Mote, so it shows the mode is on everywhere
  // while every click still lands on the page. Links get the hand, fields keep the text cursor,
  // and the cross only shows while a crop is being dragged.
  // Mia's Mote, in pink; a plain orange dot if the art didn't load.
  const FACE = (cx, cy, r) => globalThis.__ghostMote
    ? `<image href="${globalThis.__ghostMote}" x="${cx - r}" y="${cy - r}" width="${2 * r}" height="${2 * r}"/>`
    : `<circle cx="${cx}" cy="${cy}" r="${r * 0.8}" fill="#E9407F" stroke="#fff" stroke-width="1.5"/>`;
  const cursorUrl = body => `url("data:image/svg+xml,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">${body}</svg>`)}")`;
  const ARROW_CURSOR = `${cursorUrl(`<path d="M3 2.5v17.6l4.6-4.3 3.3 7.4 3.2-1.4-3.3-7.3h6.4z" fill="#111" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>${FACE(23, 23, 8.8)}`)} 3 2, default`;
  const HAND_CURSOR = `${cursorUrl(`<path d="M8.6 4.2c0-1.1.8-1.9 1.8-1.9s1.8.8 1.8 1.9v7.3h.3v-1.6c0-1 .8-1.7 1.7-1.7s1.7.7 1.7 1.7v1.9h.3v-1c0-1 .8-1.6 1.6-1.6s1.6.7 1.6 1.6v1.6h.2c0-.9.7-1.5 1.5-1.5s1.5.7 1.5 1.5v4.4c0 3.6-2.5 6.3-6 6.3h-1.9c-2 0-3.6-.9-4.7-2.5l-3.5-5.2c-.5-.8-.3-1.8.5-2.3.7-.4 1.6-.3 2.1.4l1.4 1.9z" fill="#fff" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/>${FACE(24, 24, 8)}`)} 10 2, pointer`;
  const IMMERSIVE_CSS = `html.ghost-immersive, html.ghost-immersive * { cursor: ${ARROW_CURSOR} !important; }`
    + ` html.ghost-immersive :is(a[href], a[href] *, button, button *, [role=button], [role=button] *, [role=link], [role=link] *, label, select, summary, [onclick])`
    + ` { cursor: ${HAND_CURSOR} !important; }`
    + " html.ghost-immersive :is(input:not([type=button], [type=submit], [type=checkbox], [type=radio]), textarea, [contenteditable]:not([contenteditable=false]), [contenteditable] *)"
    + " { cursor: text !important; }"
    // Repeated class: outranks the hand and text rules above while a crop is dragged.
    + " html.ghost-cropping.ghost-cropping.ghost-cropping.ghost-cropping, html.ghost-cropping.ghost-cropping.ghost-cropping.ghost-cropping * { cursor: crosshair !important; user-select: none !important; -webkit-user-select: none !important; }";

  function pageClass(name, on) {
    if (on && !document.getElementById("ghost-immersive-style")) {
      const style = Object.assign(document.createElement("style"), { id: "ghost-immersive-style", textContent: IMMERSIVE_CSS });
      (document.head || document.documentElement).append(style);
    }
    document.documentElement.classList.toggle(name, on);
  }

  const DRAG_PX = 6; // less than this between press and release is a click
  const SKIP_WAIT_MS = 600; // skip mode waits this long after a selection before asking
  let selectionByDrag = false;
  const lastDragSelection = () => selectionByDrag;

  /** Crop mode: the human drags a box over anything (a chart, a picture, a layout) and asks about it.
   *  Sticky (immersive) stays on: nothing covers the page, so clicks, links, fields and the wheel
   *  work as usual, and only a drag becomes a crop. */
  function startCrop(sticky = false) {
    if (!askHandler) throw new Error("Asking is only on pages shared with a room");
    if (cropLayer) return;
    if (!sticky && modes.immersive && immersivePaused) sticky = true; // the shortcut resumes immersive mode
    immersivePaused = false;
    hideAsk();
    cropLayer = Object.assign(document.createElement("div"), { className: sticky ? "crop sticky" : "crop" });
    const hint = Object.assign(document.createElement("div"), {
      className: "crop-hint",
      textContent: sticky ? "Immersive · drag to ask · click and scroll as usual · Esc to pause" : "Drag over what you want to ask about · Esc to cancel",
    });
    cropLayer.append(hint);
    if (sticky) setTimeout(() => hint.remove(), 3500);
    cropLayer.addEventListener("ghost-stop", () => finish());
    // Sticky listens on the window, ahead of the page; the one-off crop on its own layer.
    const source = sticky ? window : cropLayer;
    const ours = event => host && event.composedPath().includes(host); // Ghost's own boxes and cards
    let busy = false; // taking the picture: the page must hold still until it is done
    let start = null;
    let dragging = false;
    let box = null;
    // The box being dragged becomes this crop's own, so a new drag cannot take it over.
    const takeBox = () => {
      const mine = box;
      box = null;
      return mine;
    };
    const onWheel = event => {
      if (busy) {
        event.preventDefault();
        return;
      }
      if (sticky) return; // nothing covers the page: it scrolls on its own
      // The window scrolls on its own; a panel that scrolls inside the page needs a hand.
      const scroller = scrollerOf(underLayer(event.clientX, event.clientY));
      if (!scroller) return;
      event.preventDefault();
      scroller.scrollBy({ left: event.deltaX, top: event.deltaY });
    };
    // The drag starts at a place on the page, so scrolling mid-drag keeps it on the content.
    const rectOf = event => {
      const x = start.x - scrollX, y = start.y - scrollY;
      return {
        left: Math.min(x, event.clientX), top: Math.min(y, event.clientY),
        right: Math.max(x, event.clientX), bottom: Math.max(y, event.clientY),
      };
    };
    const beginDrag = () => {
      dragging = true;
      // From here on it is a crop, not a text selection or a drag of a link or picture.
      document.getSelection()?.removeAllRanges();
      if (sticky) pageClass("ghost-cropping", true);
      if (askBox) hideAsk(); // a new crop replaces a question not yet sent
      box = Object.assign(document.createElement("div"), { className: "crop-box" });
      layer().append(box);
    };
    const onDown = event => {
      if (event.button !== 0 || ours(event) && sticky) return;
      if (busy) {
        event.preventDefault();
        return;
      }
      // Double-click and drag selects word by word: in immersive mode that stays the page's.
      if (sticky && event.detail > 1) {
        start = null;
        return;
      }
      if (!sticky) event.preventDefault();
      start = { x: event.clientX + scrollX, y: event.clientY + scrollY };
      dragging = false;
    };
    const onMove = event => {
      if (!start) return;
      if (!dragging) {
        if (Math.hypot(event.clientX + scrollX - start.x, event.clientY + scrollY - start.y) < DRAG_PX) return;
        beginDrag();
      }
      event.preventDefault();
      const r = rectOf(event);
      put(box, r.left, r.top);
      Object.assign(box.style, { width: `${r.right - r.left}px`, height: `${r.bottom - r.top}px` });
    };
    // The browser's own drag of a link or a picture would swallow the crop.
    const onDragStart = event => {
      if (!start) return;
      event.preventDefault();
      if (!dragging) beginDrag();
    };
    const swallowClick = event => {
      event.preventDefault();
      event.stopPropagation();
    };
    const onUp = async event => {
      if (!start || busy) return;
      const wasDrag = dragging;
      const r = rectOf(event);
      start = null;
      dragging = false;
      if (sticky) pageClass("ghost-cropping", false);
      if (!wasDrag) {
        // A click: in immersive mode the page already has it; a one-off crop just ends.
        if (!sticky) finish();
        return;
      }
      event.preventDefault();
      // The click that follows a drag belongs to the crop, not to what it ended on.
      window.addEventListener("click", swallowClick, { capture: true, once: true });
      setTimeout(() => window.removeEventListener("click", swallowClick, { capture: true }), 0);
      const box = takeBox();
      if (!sticky) finish();
      if (r.right - r.left < 8 || r.bottom - r.top < 8) {
        box.remove();
        return;
      }
      const rect = { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.right - r.left, height: r.bottom - r.top };
      // Pin the area to its content now: the picture takes a moment, and the
      // page may move before it is done.
      const holder = areaHolder(rect);
      const anchor = holder ? areaAnchor(holder.el, holder.offset, "area")
        : rectAnchor({ x: rect.left + scrollX, y: rect.top + scrollY, w: rect.width, h: rect.height });
      const text = textIn(rect);
      // Take the picture without the box or any of Ghost's marks in it.
      busy = true;
      box.style.visibility = "hidden";
      const shown = layer().style.visibility;
      layer().style.visibility = "hidden";
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      let image = null;
      try {
        image = captureHandler ? await captureHandler({ x: rect.left, y: rect.top, w: rect.width, h: rect.height, vw: innerWidth }) : null;
      } catch {
        image = null;
      }
      layer().style.visibility = shown;
      box.style.visibility = "visible";
      busy = false;
      const target = { text, image, region: true, holder, anchor, get box() { return viewRect(anchor) || rect; } };
      if (modes.skip) {
        box.remove();
        sendTarget(target, DEFAULT_QUESTION);
        return;
      }
      openAskBox(target, box);
    };
    const listeners = [["wheel", onWheel, { capture: true, passive: false }], ["mousedown", onDown, true],
      ["mousemove", onMove, true], ["mouseup", onUp, true], ["dragstart", onDragStart, true]];
    const finish = () => {
      document.removeEventListener("keydown", onKey, true);
      for (const [type, fn, opts] of listeners) source.removeEventListener(type, fn, opts);
      pageClass("ghost-immersive", false);
      pageClass("ghost-cropping", false);
      cropLayer?.remove();
      cropLayer = null;
    };
    const onKey = event => {
      if (event.key !== "Escape" || askBox) return;
      event.stopPropagation();
      box?.remove();
      if (sticky) {
        immersivePaused = true;
        toast("Immersive paused · Alt+Shift+A to resume");
      }
      finish();
    };
    for (const [type, fn, opts] of listeners) source.addEventListener(type, fn, opts);
    document.addEventListener("keydown", onKey, true);
    if (sticky) pageClass("ghost-immersive", true);
    layer().append(cropLayer);
  }

  /** Text mode, from the keyboard: ask about what is selected now. */
  function askSelection() {
    if (!askHandler) throw new Error("Asking is only on pages shared with a room");
    const target = selectedTarget();
    if (!target) {
      toast("Select some text first, or press Alt+Shift+A to crop an area");
      return;
    }
    openAskBox(target);
  }

  function selectedTarget() {
    const selection = document.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    const text = selection.toString().replace(/\s+/g, " ").trim();
    if (text.length < 2) return null;
    const range = selection.getRangeAt(0);
    if (host && host.contains(range.commonAncestorContainer)) return null;
    const box = range.getBoundingClientRect();
    if (!box.width && !box.height) return null;
    const anchor = rectAnchor({ x: box.left + scrollX, y: box.top + scrollY, w: box.width, h: box.height });
    return { text: text.slice(0, 4000), anchor, range: range.cloneRange(), get box() { return viewRect(anchor) || box; } };
  }

  function openAskBox(target, areaBox = null) {
    hideAsk();
    cropBox = areaBox;
    askBox = document.createElement("div");
    askBox.className = "ask-box";
    if (target.image) askBox.append(Object.assign(document.createElement("img"), { src: target.image, alt: "" }));
    const input = document.createElement("textarea");
    input.rows = 2;
    input.maxLength = 600;
    input.placeholder = "Ask the bots about this…";
    const hint = Object.assign(document.createElement("div"), { className: "hint", textContent: "Enter to send · Esc to cancel" });
    askBox.append(input, hint);
    const tall = target.image ? 190 : 90;
    const top = target.box.bottom + 8 + tall > innerHeight ? Math.max(4, target.box.top - tall - 8) : target.box.bottom + 8;
    put(askBox, Math.max(4, Math.min(innerWidth - 284, target.box.left)), top);
    // Keep the page's own shortcuts from seeing what is typed here.
    for (const type of ["keydown", "keyup", "keypress", "input"]) input.addEventListener(type, e => e.stopPropagation());
    input.addEventListener("keydown", event => {
      if (event.key === "Escape") hideAsk();
      if (event.key !== "Enter" || event.shiftKey) return;
      event.preventDefault();
      const question = input.value.trim();
      if (!question) return;
      hideAsk();
      sendTarget(target, question);
    });
    layer().append(askBox);
    // On the scrolling panel, or at its place on the page.
    if (areaBox) followBox(areaBox, target.anchor, [askBox]);
    input.focus();
  }

  // Links inside a cropped area or a selection, so a bot can open and read them when asked.
  function linksIn(target) {
    const found = [];
    const seen = new Set();
    for (const a of document.querySelectorAll("a[href]")) {
      if (found.length >= 8) break;
      let href;
      try { href = new URL(a.href, location.href).href; } catch { continue; }
      if (!/^https?:/.test(href) || seen.has(href)) continue;
      let inside = false;
      if (target.range) {
        try { inside = target.range.intersectsNode(a); } catch {}
      } else {
        const r = a.getBoundingClientRect();
        const b = target.box;
        inside = r.width > 0 && r.right > b.left && r.left < b.right && r.bottom > b.top && r.top < b.bottom;
      }
      if (!inside) continue;
      seen.add(href);
      found.push({ href: href.slice(0, 1000), text: (a.innerText || a.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200) });
    }
    return found;
  }

  /** Send a question about a selection or a cropped area to the room. */
  function sendTarget(target, question) {
    const b = target.box;
    const rect = { x: b.left + scrollX, y: b.top + scrollY, w: b.width, h: b.height };
    // Long selections travel as their start and end, so any length can be found again.
    // A cropped area travels as its place on the page (or on the panel that scrolls it).
    const long = target.text.length > 400;
    const anchor = target.region
      ? (target.holder ? { selector: target.holder.selector, offset: target.holder.offset, rect } : { rect })
      : {
        text: long ? target.text.slice(0, 200) : target.text,
        ...(long ? { end: target.text.slice(-200) } : {}),
        rect };
    try {
      askHandler({ text: target.text, question: question.slice(0, 600), target: anchor, image: target.image || null, links: linksIn(target) });
    } catch {}
  }

  function offerAsk() {
    // Skip mode asks on its own for a dragged selection; a double-clicked word still gets the button.
    if (modes.skip && lastDragSelection()) return;
    askTimer = 0;
    if (askBox) return;
    const target = selectedTarget();
    hideAsk();
    if (!target) return;
    askButton = document.createElement("button");
    askButton.className = "ask-button";
    askButton.textContent = "Ask bots";
    put(askButton, Math.max(4, Math.min(innerWidth - 90, target.box.right - 40)),
      target.box.top - 36 < 4 ? target.box.bottom + 6 : target.box.top - 36);
    // Pressing the button must not clear the selection it is about.
    askButton.addEventListener("mousedown", event => event.preventDefault());
    askButton.addEventListener("click", () => openAskBox(target));
    layer().append(askButton);
  }

  /** On shared pages: show an Ask button by selected text and call handler with the question. */
  function enableAsk(handler, capture = null) {
    if (typeof handler !== "function") throw new Error("enableAsk needs a function");
    askHandler = handler;
    if (typeof capture === "function") captureHandler = capture;
    for (const card of convs.values()) addReply(card);
    if (askListening) return;
    askListening = true;
    const onSelection = () => {
      clearTimeout(askTimer);
      if (!askHandler) return;
      askTimer = setTimeout(offerAsk, 250);
    };
    const onDown = event => {
      if (event.composedPath().includes(host)) return;
      onPress(event);
      if (askBox) hideAsk();
    };
    let lastSkipped = "";
    let pressedAt = null;
    let skipTimer = 0;
    const onPress = event => {
      clearTimeout(skipTimer); // pressing again (a double-click, a new selection) calls it off
      pressedAt = { x: event.clientX, y: event.clientY };
    };
    const onUp = event => {
      if (event.composedPath().includes(host)) return;
      // A double or triple click selects a word or a line on its own: that is clicking, not asking.
      // Only a selection dragged out on purpose asks, and only once the person has let go for a moment.
      const dragged = pressedAt && Math.hypot(event.clientX - pressedAt.x, event.clientY - pressedAt.y) >= DRAG_PX;
      selectionByDrag = Boolean(dragged) && event.detail <= 1;
      if (!modes.skip || !askHandler || !selectionByDrag) return;
      clearTimeout(skipTimer);
      skipTimer = setTimeout(() => {
        const target = selectedTarget();
        if (!target || target.text === lastSkipped) return;
        lastSkipped = target.text;
        sendTarget(target, DEFAULT_QUESTION);
        toast("Asked the bots about your selection · Stop on its card");
      }, SKIP_WAIT_MS);
    };
    askListeners.push(["selectionchange", onSelection, false], ["mousedown", onDown, true], ["mouseup", onUp, true]);
    for (const [type, fn, opts] of askListeners) document.addEventListener(type, fn, opts);
  }

  const askListeners = [];

  /** A newer copy of this file takes over: stop this one's timers and listeners for good. */
  function retire() {
    clearInterval(hrefTimer);
    clearTimeout(askTimer);
    cancelAnimationFrame(frame);
    frame = 0;
    for (const [type, fn, opts] of askListeners) document.removeEventListener(type, fn, opts);
    askListeners.length = 0;
    askListening = false;
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
    const previous = actors.get(spec.actor_id)?.spec;
    if (previous) {
      // Keep the actor's look when an action only moves its target.
      for (const key of ["label", "color", "owner_color", "kind"]) if (spec[key] === undefined) spec[key] = previous[key];
    }
    const anchor = makeAnchor(spec);
    remove(spec.actor_id);
    const ttl = Number.isFinite(spec.ttl_ms) ? Math.max(1000, Math.min(spec.ttl_ms, 60 * 60 * 1000)) : DEFAULT_TTL_MS;
    const entry = { spec, anchor, nodes: makeNodes(spec), expiresAt: Date.now() + ttl, slot: 0, lastRect: null };
    const pointer = spec.pointer;
    if (pointer && typeof pointer === "object" && Number.isFinite(pointer.fx) && Number.isFinite(pointer.fy)) {
      const pa = makeAnchor({ anchor: pointer.anchor });
      if (pa.describe !== "page") {
        entry.pointer = { fx: Math.min(Math.max(pointer.fx, 0), 1), fy: Math.min(Math.max(pointer.fy, 0), 1) };
        entry.pointerAnchor = pa;
      }
    }
    actors.set(spec.actor_id, entry);
    reslot();
    render(entry);
    schedule();
    return {
      actor_id: spec.actor_id, target: anchor.describe, rect: entry.lastRect, status: spec.status || "working",
      anchor: portable(spec, anchor, entry.lastRect),
    };
  }

  function clear(actorId) {
    if (actorId) remove(actorId);
    else for (const id of [...actors.keys()]) remove(id);
    reslot();
    return { cleared: actorId || "all", remaining: actors.size };
  }

  /** The page stopped being shared: remove every mark Ghost drew and stop offering to ask. */
  function reset() {
    clear();
    for (const id of [...cards.keys()]) unsuggest(id);
    hideAsk();
    cropLayer?.dispatchEvent(new CustomEvent("ghost-stop"));
    cropLayer?.remove();
    cropLayer = null;
    for (const id of [...convs.keys()]) convs.delete(id);
    askHandler = null;
    captureHandler = null;
    return { reset: true };
  }

  function list() {
    return [...actors.values()].map(e => ({
      actor_id: e.spec.actor_id, label: e.spec.label || "", kind: e.spec.kind || "bot",
      status: e.spec.status || "working", target: e.anchor.describe, rect: e.lastRect,
    }));
  }

  // onResolve(id, decision) is set by whoever hosts the overlay (extension or Mia).
  const api = { show, clear, reset, retire, setModes, list, suggest, unsuggest, enableAsk, startCrop, askSelection, toast, onResolve: null };
  api.build = globalThis.__ghostBuild;
  globalThis.__ghostOverlay = api;
})();
