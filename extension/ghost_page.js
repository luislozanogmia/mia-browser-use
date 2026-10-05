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

  /** querySelector that also looks inside shadow roots. */
  function deepQuery(selector, root = document) {
    const found = root.querySelector(selector);
    if (found) return found;
    for (const host of root.querySelectorAll("*")) {
      const shadow = shadowOf(host);
      const inner = shadow && deepQuery(selector, shadow);
      if (inner) return inner;
    }
    return null;
  }

  /** What a node shows, in order: its shadow tree (with slotted content) or its own children. */
  function renderedChildren(node) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const shadow = shadowOf(node);
      if (shadow) return shadow.childNodes;
      if (node.tagName === "SLOT") {
        const assigned = node.assignedNodes({ flatten: true });
        if (assigned.length) return assigned;
      }
    }
    return node.childNodes;
  }

  function isVisible(node) {
    const style = getComputedStyle(node);
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
    if (tag === "a") return `[${n}] link: ${label}` + (href ? ` (${href.slice(0, 80)})` : "");
    if (tag === "input") return `[${n}] input(${type}): ${value || label}`;
    if (tag === "select") return `[${n}] select: ${label}`;
    if (tag === "textarea") return `[${n}] textarea: ${value || label}`;
    return `[${n}] ${tag}: ${label}`;
  }

  function parentOf(node) {
    if (node.parentElement) return node.parentElement;
    const root = node.getRootNode();
    return root instanceof ShadowRoot ? root.host : null;
  }

  /** The element on top at a point, inside shadow roots too. */
  function topAt(x, y) {
    let el = document.elementFromPoint(x, y);
    for (let depth = 0; el && depth < 10; depth++) {
      const inner = shadowOf(el)?.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  /** What floats on top of the page (a chat window, a dialog, a pop-up), found by looking at what's on
   * top across the screen. Sites often add these at the end of the page, past where a read stops. */
  function layersOnTop() {
    const found = new Set();
    const w = innerWidth, h = innerHeight;
    for (let i = 1; i < 12; i++) {
      for (let j = 1; j < 8; j++) {
        let layer = null;
        for (let node = topAt((w * i) / 12, (h * j) / 8); node && node !== document.body; node = parentOf(node)) {
          if (node.nodeType === Node.ELEMENT_NODE &&
              (getComputedStyle(node).position === "fixed" || ["dialog", "alertdialog"].includes(node.getAttribute("role")))) {
            layer = node;  // keep climbing: the outermost one is the whole window
          }
        }
        // Bars (a site's top menu, a minimized chat) are short; windows and dialogs are not.
        if (layer && layer.getBoundingClientRect().height >= 120) found.add(layer);
      }
    }
    // A layer inside another is read with it.
    return [...found].filter(a => ![...found].some(b => b !== a && b.contains(a)));
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
    // el.click() runs the element's handlers without moving keyboard focus.
    for (const type of ["pointerover", "mouseover", "pointerdown", "mousedown", "pointerup", "mouseup"]) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
    }
    el.click();
    return { clicked: true, tag: el.tagName.toLowerCase(), text: (el.textContent || "").trim().slice(0, 100), anchor: anchorOf(el) };
  }

  function setValue(el, value) {
    const tag = el.tagName.toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") {
      // The native setter keeps frameworks like React in sync, and no focus is needed.
      const proto = tag === "input" ? HTMLInputElement.prototype : tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLSelectElement.prototype;
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
