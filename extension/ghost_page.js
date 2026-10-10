/**
 * Ghost page helpers, shared by every browser Ghost drives.
 *
 * Each actor (bot or human) gets its own numbered element list per page, so
 * one bot's read never renumbers another bot's elements. Edits are
 * focus-free: they never keep the human's keyboard focus or text cursor (a rich editor
 * gets focus only while text goes in, then it goes back).
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

  // Label wrappers can contain a textarea's default text. Read label text while
  // omitting form controls, so an accessible name cannot expose their contents.
  function labelText(node) {
    if (!node) return "";
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || "";
    if (node.nodeType !== Node.ELEMENT_NODE ||
        ["input", "textarea", "select", "script", "style"].includes(node.tagName.toLowerCase()) ||
        node.isContentEditable) return "";
    return Array.from(node.childNodes || [], labelText).join(" ");
  }

  function fieldLabel(node, tag) {
    const clean = value => (value || "").replace(/\s+/g, " ").trim().slice(0, 100);
    const aria = clean(node.getAttribute("aria-label"));
    if (aria) return aria;
    // ID references belong to the control's document or shadow root, not the
    // top-level page (frames and shadow trees may reuse the same IDs).
    const root = node.getRootNode();
    const refs = (node.getAttribute("aria-labelledby") || "").trim().split(/\s+/);
    const named = clean(refs.map(id => labelText(root.getElementById?.(id))).join(" "));
    if (named) return named;
    const associated = clean(Array.from(node.labels || [], labelText).join(" "));
    return associated || clean(node.getAttribute("placeholder")) || clean(node.getAttribute("title")) || tag;
  }

  function controlState(node) {
    return node.disabled || node.getAttribute("disabled") !== null ||
      node.getAttribute("aria-disabled") === "true" || node.matches?.(":disabled") ? " [disabled]" : "";
  }

  function describe(node, tag, n) {
    const field = tag === "input" || tag === "textarea";
    const label = field ? fieldLabel(node, tag) :
      (node.textContent || "").trim().slice(0, 100) || node.getAttribute("aria-label") ||
      node.getAttribute("placeholder") || node.getAttribute("title") || tag;
    const href = node.getAttribute("href") || "";
    const type = node.getAttribute("type") || "";
    // Current form values can hold credentials; list the control, never its value.
    const value = (tag === "input" || tag === "textarea") && node.value ? "[REDACTED]" : "";
    const state = controlState(node);
    if (tag === "a") return `[${n}] link: ${label}${state}` + (href ? ` (${href.slice(0, 200)})` : "");
    if (tag === "input") return `[${n}] input(${type}): ${label}` + (value ? ` ${value}` : "") + state;
    if (tag === "select") return `[${n}] select: ${label}${state}`;
    if (tag === "textarea") return `[${n}] textarea: ${label}` + (value ? ` ${value}` : "") + state;
    return `[${n}] ${tag}: ${label}${state}`;
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

  // Selector copies need the browser's rendered spacing: inline links are not
  // separate paragraphs. Only use innerText for plain, read-only light DOM;
  // control/editor contents must retain the enumerator's redaction behavior.
  function selectorText(root, maxChars) {
    for (let ancestor = root; ancestor; ancestor = parentOf(ancestor)) {
      if (!isVisible(ancestor)) return "";
      if (ancestor.isContentEditable) return null;
    }
    const pending = [root];
    let visited = 0;
    while (pending.length) {
      const node = pending.pop();
      if (++visited > 10000) return null;
      const tag = node.tagName.toLowerCase();
      if (["input", "textarea", "select", "script", "style", "noscript", "svg",
           "ghost-overlay", "iframe", "frame"].includes(tag) ||
          node.isContentEditable || shadowOf(node)) return null;
      for (const child of node.children || []) pending.push(child);
    }
    return typeof root.innerText === "string" ? root.innerText.trim().slice(0, maxChars) : null;
  }

  /** Walk the page, number interactive elements for this actor, return text. */
  function enumerate(actor, maxChars, selector) {
    checkActor(actor);
    const rootEl = selector ? deepQuery(selector) : document.body;
    if (!rootEl) fail("NOT_FOUND", `Selector "${selector}" not found`);
    const items = [];
    const elements = [];
    let target = null;
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
        const line = container ? `[${n}] ${tag}: ${(node.getAttribute("aria-label") || node.getAttribute("title") || "area").slice(0, 100)}${controlState(node)}`
          : describe(node, tag, n);
        items.push(line);
        if (selector && node === rootEl) target = { choice: n, line: line.replace(/^\[\d+\] /, "") };
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
    return { text: items.join("\n"), count: elements.filter(Boolean).length, snapshot, target,
      rendered_text: selector ? selectorText(rootEl, maxChars) : null };
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
      typeIntoEditor(el, value);
      return;  // the editor sent its own input events
    } else {
      fail("NOT_EDITABLE", `<${tag}> is not an input, textarea, select or editable element`);
    }
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertReplacementText" }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /** Rich editors (LinkedIn's message box, Gmail, Slack) keep their own model of the text and only see
   * what comes through the browser's text input: replacing the element's text left LinkedIn's
   * placeholder drawn over it and Send off. Insert it the way typing or pasting does, then hand the
   * keyboard back to wherever it was. */
  function typeIntoEditor(el, value) {
    const doc = el.ownerDocument, win = doc.defaultView;
    const before = doc.activeElement;
    el.focus({ preventScroll: true });
    const range = doc.createRange();
    range.selectNodeContents(el);
    const selection = win.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    // Empty it first, the way the editor itself deletes (a selection inside a chat window's shadow
    // root may cover only part of an old draft); if that leaves text, clear it by hand.
    doc.execCommand("selectAll");
    doc.execCommand("delete");
    if (el.textContent.trim()) {
      el.replaceChildren(doc.createElement("p"));
      el.firstChild.append(doc.createElement("br"));
      el.dispatchEvent(new win.InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
    }
    selection.removeAllRanges();
    range.selectNodeContents(el.firstChild || el);
    range.collapse(true);
    selection.addRange(range);
    const lines = value.split("\n");
    // Exactly the value (ignoring spacing) with its line breaks: nothing of an old draft left around it.
    const bare = text => text.replace(/\s+/g, "");
    const exact = () => bare(el.textContent) === bare(value);
    const kept = () => exact() && (el.innerText.match(/\n/g) || []).length >= lines.length - 1;
    let typed = false;
    if (lines.length > 1) {
      // Several lines: paste them the way the person would, so editors (LinkedIn's) keep every line
      // break and blank line. Editors that ignore a paste get each line typed with Enter between.
      const data = new win.DataTransfer();
      data.setData("text/plain", value);
      el.dispatchEvent(new win.ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
      typed = kept();
      if (!typed) {
        selection.removeAllRanges();
        range.selectNodeContents(el);
        selection.addRange(range);
        doc.execCommand("delete");
        lines.forEach((line, i) => {
          if (i) doc.execCommand("insertParagraph");
          if (line) doc.execCommand("insertText", false, line);
        });
        typed = kept();
      }
    } else {
      typed = doc.execCommand("insertText", false, value);
    }
    if (!typed || !exact()) {
      // No text input here: one paragraph per line, as editors keep them.
      el.replaceChildren(...value.split("\n").map(line => {
        const p = doc.createElement("p");
        if (line) p.textContent = line; else p.append(doc.createElement("br"));
        return p;
      }));
      el.dispatchEvent(new win.InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    }
    selection.removeAllRanges();
    if (before && before !== el && before !== doc.body && typeof before.focus === "function") {
      before.focus({ preventScroll: true });
    } else {
      el.blur();
    }
  }

  function fill(actor, choice, selector, value) {
    const el = resolve(actor, choice, selector);
    setValue(el, String(value));
    return { filled: true, tag: el.tagName.toLowerCase(), anchor: anchorOf(el) };
  }

  /** Append text to one element without focusing it. */
  function typeInto(actor, choice, selector, text) {
    const el = resolve(actor, choice, selector);
    // innerText keeps the line breaks the editor already has (textContent runs paragraphs together).
    const current = el.isContentEditable ? el.innerText.replace(/\n$/, "") : (el.value ?? "");
    setValue(el, current + String(text));
    return { typed: true, characters: String(text).length, tag: el.tagName.toLowerCase(), anchor: anchorOf(el) };
  }

  /** Where the human is working in this page, for their presence. */
  function humanFocus() {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) return null;
    return anchorOf(el);
  }

  /** Complete, bounded visible records for a frozen destination witness.
   * Light-DOM only. Ambiguity and omission indications fail closed; this does
   * not infer that a site's declared total is truthful or transactionally stable.
   */
  function records(actor, spec) {
    checkActor(actor);
    const hold = message => fail("RECOVERY_HOLD", message);
    const keys = ["collection", "row", "id", "identity", "fields", "total_count"];
    if (!spec || typeof spec !== "object" || Array.isArray(spec) ||
        Object.keys(spec).sort().join() !== keys.sort().join()) hold("invalid record selector specification");
    const selector = value => {
      if (typeof value !== "string" || !value.trim() || value.length > 500) hold("invalid record selector");
      return value;
    };
    for (const key of ["collection", "row", "id", "identity", "total_count"]) selector(spec[key]);
    if (!spec.fields || typeof spec.fields !== "object" || Array.isArray(spec.fields) ||
        !Object.keys(spec.fields).length || Object.keys(spec.fields).length > 32) hold("invalid field selectors");
    for (const [name, value] of Object.entries(spec.fields)) {
      if (!name.trim() || name.length > 100 || /[\x00-\x1f\x7f]/.test(name)) hold("invalid field name");
      selector(value);
    }
    const query = (root, css) => {
      try { return Array.from(root.querySelectorAll(css)); }
      catch { hold("invalid CSS selector"); }
    };
    const visible = el => {
      for (let node = el; node && node.nodeType === Node.ELEMENT_NODE; node = node.parentElement) {
        const style = styleOf(node);
        if (node.hidden || node.getAttribute("aria-hidden") === "true" || style.display === "none" ||
            style.visibility === "hidden" || style.visibility === "collapse" || Number(style.opacity) === 0) return false;
      }
      return el.getClientRects().length > 0;
    };
    const one = (root, css) => {
      const matches = query(root, css);
      if (matches.length !== 1 || !visible(matches[0])) hold("record selector must identify one visible element");
      return matches[0];
    };
    let textBytes = 0, nodes = 0;
    const text = el => {
      let out = "";
      const visit = node => {
        if (++nodes > 50000) hold("record node limit exceeded");
        if (node.nodeType === Node.TEXT_NODE) {
          if (out.length + node.textContent.length > 4096) hold("record text limit exceeded");
          out += node.textContent;
          return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"].includes(node.tagName)) return;
        if (["INPUT", "TEXTAREA", "SELECT", "IFRAME", "FRAME"].includes(node.tagName) ||
            node.isContentEditable || node.hasAttribute("contenteditable") || shadowOf(node)) hold("editable or unsupported record text");
        if (!visible(node)) hold("hidden record text cannot be evidence");
        for (const child of node.childNodes) visit(child);
      };
      visit(el);
      if (out.length > 4096 || (textBytes += out.length) > 1000000) hold("record text limit exceeded");
      return out.trim();
    };
    if (document.readyState !== "complete" || query(document, '[aria-busy="true"]').length) hold("destination is still loading");
    const collection = one(document, spec.collection), countElement = one(document, spec.total_count);
    const totalText = text(countElement);
    if (!/^(0|[1-9][0-9]*)$/.test(totalText)) hold("total count must be an exact nonnegative integer");
    const total = Number(totalText);
    if (!Number.isSafeInteger(total) || total > 1000) hold("record count limit exceeded");
    const descendants = query(collection, "*");
    if (descendants.length > 50000) hold("record collection node limit exceeded");
    for (const node of [collection, ...descendants]) {
      if (shadowOf(node) || ["IFRAME", "FRAME"].includes(node.tagName)) hold("unsupported collection tree");
      if (node.hasAttribute("data-virtualized") && node.getAttribute("data-virtualized") !== "false") hold("virtualized collection");
      for (const key of ["aria-rowcount", "aria-setsize"]) {
        const value = node.getAttribute(key);
        if (value !== null && value !== String(total)) hold("collection size indicates omitted records");
      }
    }
    const rows = query(collection, spec.row);
    if (rows.length !== total) hold("observed rows do not equal destination total");
    const ids = new Set(), result = [];
    for (const [index, row] of rows.entries()) {
      if (!visible(row)) hold("hidden row cannot establish complete coverage");
      for (const key of ["aria-rowindex", "aria-posinset"]) {
        const value = row.getAttribute(key);
        if (value !== null && value !== String(index + 1)) hold("row positions indicate omissions");
      }
      const id = text(one(row, spec.id)), identity = text(one(row, spec.identity));
      if (!id || !identity || /[\x00-\x1f\x7f]/.test(id + identity) || ids.has(id)) hold("empty, malformed, or duplicate record identity");
      ids.add(id);
      const fields = Object.create(null);
      for (const [name, css] of Object.entries(spec.fields)) fields[name] = text(one(row, css));
      result.push({ id, identity, fields });
    }
    return { destination_url: location.href, complete: true, records: result };
  }

  globalThis.__ghostPage = { enumerate, resolve, deepQuery, anchorOf, cssPath, click, fill, typeInto, humanFocus, records, build: globalThis.__ghostBuild };
})();
