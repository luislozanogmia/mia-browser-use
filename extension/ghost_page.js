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
  if (globalThis.__ghostPage) return;

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

  function isVisible(node) {
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden";
  }

  function isInteractive(node, tag) {
    return tag === "a" || tag === "button" || tag === "input" || tag === "select" || tag === "textarea" ||
      node.getAttribute("role") === "button" || node.hasAttribute("onclick") || node.hasAttribute("tabindex") ||
      node.isContentEditable && !node.parentElement?.isContentEditable;
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

  /** Walk the page, number interactive elements for this actor, return text. */
  function enumerate(actor, maxChars, selector) {
    checkActor(actor);
    const rootEl = selector ? document.querySelector(selector) : document.body;
    if (!rootEl) fail("NOT_FOUND", `Selector "${selector}" not found`);
    const items = [];
    const elements = [];
    let chars = 0;

    function walk(node) {
      if (chars >= maxChars) return;
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
        const line = describe(node, tag, n);
        items.push(line);
        chars += line.length;
        return;
      }
      for (const child of node.childNodes) {
        if (chars >= maxChars) break;
        walk(child);
      }
    }

    walk(rootEl);
    const snapshot = `${actor}-${++snapshotCounter}`;
    lists.set(actor, { snapshot, elements });
    return { text: items.join("\n"), count: elements.filter(Boolean).length, snapshot };
  }

  /** Find the element an actor means: its own number, or a selector. */
  function resolve(actor, choice, selector) {
    if (typeof selector === "string" && selector) {
      const el = document.querySelector(selector);
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
    if (el.id && document.querySelectorAll(`#${CSS.escape(el.id)}`).length === 1) return `#${CSS.escape(el.id)}`;
    const parts = [];
    for (let node = el; node && node.nodeType === Node.ELEMENT_NODE && node !== document.documentElement; node = node.parentElement) {
      if (node.id && document.querySelectorAll(`#${CSS.escape(node.id)}`).length === 1) {
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

  globalThis.__ghostPage = { enumerate, resolve, anchorOf, cssPath, click, fill, typeInto, humanFocus };
})();
