/**
 * Ghost page helpers, shared by every browser Ghost drives.
 *
 * Each actor (bot or human) gets its own numbered element list per page, so
 * one bot's read never renumbers another bot's elements. Edits are
 * focus-free: they never move the human's keyboard focus or text cursor.
 *
 * The Chrome extension injects this file into its isolated world, and the Mia
 * in-app browser can run it in the page. It keeps no state outside the page.
 */
(() => {
  if (globalThis.__ghostPage && globalThis.__ghostPage.build === globalThis.__ghostBuild) return;

  const ACTOR_RE = /^[A-Za-z0-9_.:-]{1,64}$/;
  const lists = new Map(); // actor_id -> {snapshot, elements: []}
  let snapshotCounter = 0;

  function fail(code, message) {
    const err = new Error(`${code}: ${message}`);
    err.code = code;
    throw err;
  }

  function checkActor(actor) {
    if (typeof actor !== "string" || !ACTOR_RE.test(actor)) fail("INVALID_ACTOR", "actor_id must be 1-64 of A-Z a-z 0-9 _ . : -");
    return actor;
  }

  // Sites like LinkedIn draw parts of the page (its message window) inside shadow roots, which
  // document.querySelector and childNodes don't reach. The extension can open closed ones too.
  function shadowOf(node) {
    try {
      return globalThis.chrome?.dom?.openOrClosedShadowRoot?.(node) || node.shadowRoot || null;
    } catch {
      return node.shadowRoot || null;
    }
  }

  /** A frame's page when it's from the same site (LinkedIn draws its messaging in one), else null. */
  function frameDoc(node) {
    if (node.tagName !== "IFRAME" && node.tagName !== "FRAME") return null;
    try {
      return node.contentDocument?.documentElement ? node.contentDocument : null;
    } catch {
      return null;  // another site's frame: not readable, and not ours to read
    }
  }

  /** querySelector that also looks inside shadow roots and same-site frames. */
  function deepQuery(selector, root = document) {
    const found = root.querySelector(selector);
    if (found) return found;
    for (const host of root.querySelectorAll("*")) {
      const inside = shadowOf(host) || frameDoc(host);
      const inner = inside && deepQuery(selector, inside);
      if (inner) return inner;
    }
    return null;
  }

  function styleOf(node) {
    return (node.ownerDocument?.defaultView || window).getComputedStyle(node);
  }

  /** What a node shows, in order: its shadow tree (with slotted content), a frame's page, or its own children. */
  function renderedChildren(node) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const shadow = shadowOf(node);
      if (shadow) return shadow.childNodes;
      const doc = frameDoc(node);
      if (doc) {
        // A frame kept behind the page (LinkedIn preloads a hidden copy of itself) isn't shown: skip it.
        const r = node.getBoundingClientRect(), z = parseInt(styleOf(node).zIndex, 10);
        return doc.body && r.width > 1 && r.height > 1 && !(z < 0) ? [doc.body] : [];
      }
      if (node.tagName === "SLOT") {
        const assigned = node.assignedNodes({ flatten: true });
        if (assigned.length) return assigned;
      }
    }
    return node.childNodes;
  }

  function isVisible(node) {
    const style = styleOf(node);
    return style.display !== "none" && style.visibility !== "hidden";
  }

  const CONTROLS = new Set(["a", "button", "input", "select", "textarea"]);
  const NESTED = "a[href], button, input, select, textarea, [role=button], [onclick], [tabindex]:not([tabindex='-1'])";

  function isInteractive(node, tag) {
    // tabindex="-1" only makes a region focusable from script (skip links jump to <main> that way): not a control.
    const tabindex = node.getAttribute("tabindex");
    return CONTROLS.has(tag) || node.getAttribute("role") === "button" || node.hasAttribute("onclick") ||
      tabindex !== null && tabindex !== "-1" || node.isContentEditable && !node.parentElement?.isContentEditable;
  }

  // A clickable wrapper (a card, a list) that holds its own controls or a lot of text: number it, and read inside.
  function isContainer(node, tag) {
    return !CONTROLS.has(tag) && !node.isContentEditable &&
      (node.querySelector(NESTED) !== null || (node.textContent || "").length > 300);
  }

  function describe(node, tag, n) {
    const label = (node.textContent || "").trim().slice(0, 100) || node.getAttribute("aria-label") ||
      node.getAttribute("placeholder") || node.getAttribute("title") || tag;
    const href = node.getAttribute("href") || "";
    const type = node.getAttribute("type") || "";
    // Current form values can hold credentials; list the control, never its value.
    const value = (tag === "input" || tag === "textarea") && node.value ? "[REDACTED]" : "";
    if (tag === "a") return `[${n}] link: ${label}` + (href ? ` (${href.slice(0, 200)})` : "");
    if (tag === "input") return `[${n}] input(${type}): ${value || label}`;
    if (tag === "select") return `[${n}] select: ${label}`;
    if (tag === "textarea") return `[${n}] textarea: ${value || label}`;
    return `[${n}] ${tag}: ${label}`;
  }

  function parentOf(node) {
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode();
    if (root.host) return root.host;  // out of a shadow root
    return root.defaultView?.frameElement || null;  // out of a frame
  }

  /** The element on top at a point, inside shadow roots too. */
  function topAt(x, y) {
    let el = document.elementFromPoint(x, y);
    for (let depth = 0; el && depth < 10; depth++) {
      let inner = shadowOf(el)?.elementFromPoint(x, y);
      const doc = !inner && frameDoc(el);
      if (doc) {  // into the frame, in its own coordinates
        const r = el.getBoundingClientRect();
        x -= r.left; y -= r.top;
        inner = doc.elementFromPoint(x, y);
      }
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  /** What floats on top of the page (a chat window, a dialog, a pop-up), found by looking at what's on
   * top across the screen. Sites often add these at the end of the page, past where a read stops. */
  function layersOnTop() {
    const found = new Set();
    const blocks = new Map();  // the page's top-level block under each point -> how often
    const floating = new Set();  // blocks where what's under the point floats (fixed or absolute)
    const tall = node => node.getBoundingClientRect().height >= 120;  // not a bar (a top menu, a minimized chat)
    const w = innerWidth, h = innerHeight;
    for (let i = 1; i < 12; i++) {
      for (let j = 1; j < 8; j++) {
        let fixed = null, lastTall = null, block = null, floats = false;
        for (let node = topAt((w * i) / 12, (h * j) / 8); node && node !== document.body && node !== document.documentElement;
             node = parentOf(node)) {
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          if (tall(node)) lastTall = node;
          const position = styleOf(node).position;
          if (position === "fixed" || position === "absolute") floats = true;
          if (position === "fixed" || ["dialog", "alertdialog"].includes(node.getAttribute("role"))) {
            // The outermost one is the whole window; a short fixed holder stands for the tall box inside it.
            fixed = tall(node) ? node : lastTall || fixed;
          }
          if (node.parentElement === document.body) block = node;
        }
        if (fixed) found.add(fixed);
        if (block) blocks.set(block, (blocks.get(block) || 0) + 1);
        if (block && floats) floating.add(block);
      }
    }
    // Windows, chats and dialogs are usually their own block next to the page's main one (LinkedIn's
    // message window is): another block on screen whose part there floats is on top of the page.
    // (A side menu that's just part of the layout doesn't float.)
    const main = [...blocks].sort((a, b) => b[1] - a[1])[0]?.[0];
    for (const block of floating) {
      if (block !== main && tall(block)) found.add(block);
    }
    // A layer inside another is read with it.
    return [...found].filter(a => a !== main && ![...found].some(b => b !== a && deepContains(b, a)));
  }

  function deepContains(outer, node) {
    for (let n = node; n; n = parentOf(n)) if (n === outer) return true;
    return false;
  }

  /** Walk the page, number interactive elements for this actor, return text. */
  function enumerate(actor, maxChars, selector) {
    checkActor(actor);
    const rootEl = selector ? deepQuery(selector) : document.body;
    if (!rootEl) fail("NOT_FOUND", `Selector "${selector}" not found`);
    const items = [];
    const elements = [];
    let chars = 0;
    let skip = new Set();  // layers already read

    function walk(node) {
      if (chars >= maxChars || skip.has(node)) return;
      if (node.nodeType === Node.TEXT_NODE) {
        const text = node.textContent.trim();
        if (text) { items.push(text); chars += text.length; }
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const tag = node.tagName.toLowerCase();
      if (["script", "style", "noscript", "svg", "ghost-overlay"].includes(tag)) return;
      if (!isVisible(node)) return;
      if (isInteractive(node, tag)) {
        const n = items.length;
        elements[n] = node;
        const container = isContainer(node, tag);
        // A container's text follows below, so its own line only names it.
        const line = container ? `[${n}] ${tag}: ${(node.getAttribute("aria-label") || node.getAttribute("title") || "area").slice(0, 100)}`
          : describe(node, tag, n);
        items.push(line);
        chars += line.length;
        if (!container) return;
      }
      for (const child of renderedChildren(node)) {
        if (chars >= maxChars) break;
        walk(child);
      }
    }

    if (selector) {
      walk(rootEl);
    } else {
      // What's on top first: it's what the person is looking at, and it may be past where the read stops.
      const layers = layersOnTop();
      if (layers.length) {
        items.push("On top of the page (a window or dialog):");
        for (const layer of layers) walk(layer);
        items.push("The page under it:");
        skip = new Set(layers);
      }
      walk(rootEl);
    }
    const snapshot = `${actor}-${++snapshotCounter}`;
    lists.set(actor, { snapshot, elements });
    return { text: items.join("\n"), count: elements.filter(Boolean).length, snapshot };
  }

  /** Find the element an actor means: its own number, or a selector. */
  function resolve(actor, choice, selector) {
    if (typeof selector === "string" && selector) {
      const el = deepQuery(selector);
      if (!el) fail("NOT_FOUND", `Selector not found: ${selector}`);
      return el;
    }
    if (Number.isInteger(choice)) {
      const list = lists.get(checkActor(actor));
      if (!list) fail("NO_ELEMENTS", "Read or vacuum this tab first to number its elements");
      const el = list.elements[choice];
      if (!el) fail("NOT_FOUND", `Element ${choice} is not in your list`);
      if (!el.isConnected) fail("STALE_ELEMENT", `Element ${choice} is gone; read the page again`);
      return el;
    }
    fail("NO_TARGET", "Provide choice (number) or selector");
  }

  /** A CSS path that another browser showing the same page can resolve. */
  function cssPath(el) {
    // Inside a shadow root the path starts at that root; deepQuery finds it there again.
    const scope = el.getRootNode();
    if (el.id && scope.querySelectorAll(`#${CSS.escape(el.id)}`).length === 1) return `#${CSS.escape(el.id)}`;
    const parts = [];
    for (let node = el; node && node.nodeType === Node.ELEMENT_NODE && node !== document.documentElement; node = node.parentElement) {
      if (node.id && scope.querySelectorAll(`#${CSS.escape(node.id)}`).length === 1) {
        parts.unshift(`#${CSS.escape(node.id)}`);
        break;
      }
      const tag = node.tagName.toLowerCase();
      const siblings = node.parentElement ? [...node.parentElement.children].filter(c => c.tagName === node.tagName) : [];
      parts.unshift(siblings.length > 1 ? `${tag}:nth-of-type(${siblings.indexOf(node) + 1})` : tag);
    }
    return parts.join(" > ");
  }

  // Their text can be what someone typed (a textarea's content), so anchors never carry it.
  const FORM_CONTROLS = new Set(["input", "textarea", "select", "option"]);

  /** Portable description of an element: works in other people's browsers. */
  function anchorOf(el) {
    const r = el.getBoundingClientRect();
    const own = FORM_CONTROLS.has(el.tagName.toLowerCase()) ? "" : el.textContent;
    return {
      selector: cssPath(el),
      text: (own || el.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 120),
      rect: { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height },
    };
  }

  function click(actor, choice, selector) {
    const el = resolve(actor, choice, selector);
    // A click the way a mouse makes one: at the element's middle, with pointer events. Sites like
    // LinkedIn only handle clicks that look like that (a bare el.click() on its Message button follows
    // the link to the Messaging page instead of opening the chat on the profile). No focus moves.
    const win = el.ownerDocument?.defaultView || window;
    const r = el.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, composed: true, view: win, button: 0,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    const pointer = { ...at, pointerId: 1, pointerType: "mouse", isPrimary: true };
    el.dispatchEvent(new win.PointerEvent("pointerover", pointer));
    el.dispatchEvent(new win.MouseEvent("mouseover", at));
    el.dispatchEvent(new win.PointerEvent("pointermove", pointer));
    el.dispatchEvent(new win.MouseEvent("mousemove", at));
    el.dispatchEvent(new win.PointerEvent("pointerdown", { ...pointer, buttons: 1 }));
    el.dispatchEvent(new win.MouseEvent("mousedown", { ...at, buttons: 1, detail: 1 }));
    el.dispatchEvent(new win.PointerEvent("pointerup", pointer));
    el.dispatchEvent(new win.MouseEvent("mouseup", { ...at, detail: 1 }));
    // The click itself runs the element's default too (follows a link, ticks a box) unless the site
    // handles it, like el.click() did.
    el.dispatchEvent(new win.MouseEvent("click", { ...at, detail: 1 }));
    return { clicked: true, tag: el.tagName.toLowerCase(), text: (el.textContent || "").trim().slice(0, 100), anchor: anchorOf(el) };
  }

  function setValue(el, value) {
    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") {
      // The native setter keeps frameworks like React in sync, and no focus is needed.
      const win = el.ownerDocument?.defaultView || window;  // a frame's element uses its frame's setters
      const proto = tag === "input" ? win.HTMLInputElement.prototype : tag === "textarea" ? win.HTMLTextAreaElement.prototype : win.HTMLSelectElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    } else if (el.isContentEditable) {
      el.textContent = value;
    } else {
      fail("NOT_EDITABLE", `<${tag}> is not an input, textarea, select or editable element`);
    }
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertReplacementText" }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fill(actor, choice, selector, value) {
    const el = resolve(actor, choice, selector);
    setValue(el, String(value));
    return { filled: true, tag: el.tagName.toLowerCase(), anchor: anchorOf(el) };
  }

  /** Append text to one element without focusing it. */
  function typeInto(actor, choice, selector, text) {
    const el = resolve(actor, choice, selector);
    const current = el.isContentEditable ? el.textContent : (el.value ?? "");
    setValue(el, current + String(text));
    return { typed: true, characters: String(text).length, tag: el.tagName.toLowerCase(), anchor: anchorOf(el) };
  }

  /** Where the human is working in this page, for their presence. */
  function humanFocus() {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) return null;
    return anchorOf(el);
  }

  globalThis.__ghostPage = { enumerate, resolve, deepQuery, anchorOf, cssPath, click, fill, typeInto, humanFocus, build: globalThis.__ghostBuild };
})();
