/**
 * Ghost Bridge — Background Service Worker
 *
 * Connects to the Ghost bridge over an authenticated WebSocket.
 * Receives commands, executes them via Chrome APIs, returns results.
 */

const DEFAULT_PORT = 9377; // GHOST on a phone keypad
const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_DELAY = 30000;

let ws = null;
let reconnectDelay = RECONNECT_DELAY;
let reconnectTimer = null;
let connected = false;
let port = DEFAULT_PORT;
let token = "";
let intentionallyDisconnected = false;

function randomHex(bytes = 32) {
  const data = new Uint8Array(bytes);
  crypto.getRandomValues(data);
  return [...data].map(value => value.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(secret, message) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return [...new Uint8Array(signature)].map(value => value.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) return false;
  let different = 0;
  for (let index = 0; index < left.length; index += 1) {
    different |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return different === 0;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

function getStatus() {
  return { connected, paired: Boolean(token), port, version: chrome.runtime.getManifest().version };
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
}

// ---------------------------------------------------------------------------
// WebSocket connection to ghost-cli daemon
// ---------------------------------------------------------------------------

function connect() {
  if (!token) {
    setBadge("PAIR", "#f59e0b");
    return;
  }
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

  intentionallyDisconnected = false;
  let socket;
  let authState = null;
  try {
    socket = new WebSocket(`ws://127.0.0.1:${port}/ghost-bridge`);
    ws = socket;
  } catch (err) {
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    const clientNonce = randomHex();
    authState = { clientNonce, serverNonce: "", serverVerified: false };
    socket.send(JSON.stringify({ type: "auth_init", client_nonce: clientNonce }));
  };

  socket.onmessage = async (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    if (msg.type === "auth_challenge" && authState && !connected) {
      const serverNonce = typeof msg.server_nonce === "string" ? msg.server_nonce : "";
      if (!/^[0-9a-f]{64}$/.test(serverNonce)) {
        socket.close(4003, "invalid server challenge");
        return;
      }
      const expected = await hmacHex(
        token,
        `ghost-ws-server-v1:${authState.clientNonce}:${serverNonce}`,
      );
      if (!constantTimeEqual(msg.server_proof, expected)) {
        socket.close(4003, "untrusted bridge server");
        return;
      }
      authState.serverNonce = serverNonce;
      authState.serverVerified = true;
      const clientProof = await hmacHex(
        token,
        `ghost-ws-client-v1:${authState.clientNonce}:${serverNonce}`,
      );
      socket.send(JSON.stringify({ type: "auth_response", client_proof: clientProof }));
      return;
    }
    if (msg.type === "authenticated") {
      if (!authState?.serverVerified) {
        socket.close(4003, "bridge server was not authenticated");
        return;
      }
      connected = true;
      reconnectDelay = RECONNECT_DELAY;
      setBadge("ON", "#22c55e");
      socket.send(JSON.stringify({ type: "hello", source: "ghost-bridge", version: chrome.runtime.getManifest().version }));
      return;
    }
    if (!connected) return;
    if (!msg.id || !msg.command) return;

    try {
      const result = await handleCommand(msg.command, msg.args || {});
      socket.send(JSON.stringify({ id: msg.id, result }));
    } catch (err) {
      socket.send(JSON.stringify({ id: msg.id, error: err.message || String(err) }));
    }
  };

  socket.onclose = () => {
    if (ws !== socket) return;
    ws = null;
    connected = false;
    setBadge(token ? "OFF" : "PAIR", token ? "#ef4444" : "#f59e0b");
    console.log("[ghost-bridge] disconnected");
    if (!intentionallyDisconnected && token) scheduleReconnect();
  };

  socket.onerror = () => {
    // onclose will fire after this
  };
}

function disconnect({ forgetToken = false } = {}) {
  intentionallyDisconnected = true;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  const current = ws;
  ws = null;
  if (current) current.close();
  connected = false;
  if (forgetToken) {
    token = "";
    chrome.storage.local.remove("token");
  }
  setBadge(token ? "OFF" : "PAIR", token ? "#ef4444" : "#f59e0b");
}

function scheduleReconnect() {
  if (intentionallyDisconnected || !token) return;
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
}

// ---------------------------------------------------------------------------
// Command router — maps ghost-cli tool names to Chrome APIs
// ---------------------------------------------------------------------------

async function handleCommand(command, args) {
  switch (command) {
    case "ping":
      return { pong: true, ts: Date.now() };

    case "ghost_tab_list":
      return tabList(args);

    case "ghost_tab_open":
      return tabOpen(args);

    case "ghost_tab_switch":
      return tabSwitch(args);

    case "ghost_tab_close":
      return tabClose(args);

    case "ghost_navigate":
    case "ghost_vacuum":
      return navigate(args);

    case "ghost_read":
      return readPage(args);

    case "ghost_pdf_fetch":
      return fetchPdf(args);

    case "ghost_click":
      return click(args);

    case "ghost_fill":
      return fill(args);

    case "ghost_key":
      return sendKey(args);

    case "ghost_eval":
      return evaluate(args);

    case "ghost_extract":
      return extract(args);

    case "ghost_screenshot":
      return screenshot(args);

    case "ghost_scroll":
      return scroll(args);

    case "ghost_wait":
      return wait(args);

    case "ghost_show":
      return showPresence(args);

    default:
      throw new Error(`Unknown command: ${command}`);
  }
}

// ---------------------------------------------------------------------------
// Tab management
// ---------------------------------------------------------------------------

async function tabList() {
  const tabs = await chrome.tabs.query({});
  const focusedWindow = await chrome.windows.getLastFocused().catch(() => null);
  return {
    tabs: tabs.map((t, i) => ({
      index: i,
      id: t.id,
      url: t.url,
      title: t.title,
      active: t.active,
      focused: Boolean(focusedWindow && t.windowId === focusedWindow.id),
      windowId: t.windowId,
    })),
  };
}

async function tabOpen(args) {
  const tab = await chrome.tabs.create({ url: args.url || "about:blank", active: args.active !== false });
  return { id: tab.id, url: tab.url, title: tab.title };
}

async function tabSwitch(args) {
  let tabId;
  if (args.tab_id) {
    tabId = args.tab_id;
  } else if (args.tab_index !== undefined) {
    const tabs = await chrome.tabs.query({});
    const tab = tabs[args.tab_index];
    if (!tab) throw new Error(`Tab index ${args.tab_index} not found (${tabs.length} tabs open)`);
    tabId = tab.id;
  } else {
    throw new Error("Provide tab_id or tab_index");
  }
  const tab = await chrome.tabs.update(tabId, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
  return { id: tab.id, url: tab.url, title: tab.title };
}

async function tabClose(args) {
  if (args.tab_id) {
    await chrome.tabs.remove(args.tab_id);
  } else if (args.tab_index !== undefined) {
    const tabs = await chrome.tabs.query({});
    const tab = tabs[args.tab_index];
    if (!tab) throw new Error(`Tab index ${args.tab_index} not found`);
    await chrome.tabs.remove(tab.id);
  } else {
    throw new Error("Provide tab_id or tab_index");
  }
  return { closed: true };
}

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

async function getActiveTabId(args) {
  if (args.tab_id) return args.tab_id;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("No active tab");
  return tab.id;
}

async function navigate(args) {
  const url = args.url;
  if (!url) throw new Error("url is required");

  let tabId;
  if (args.tab_id) {
    tabId = args.tab_id;
  } else {
    // Use active tab or create new one
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    tabId = active ? active.id : (await chrome.tabs.create({ url })).id;
  }

  await chrome.tabs.update(tabId, { url, active: true });

  // Wait for load
  await waitForTabLoad(tabId, args.timeout || 30000);

  const tab = await chrome.tabs.get(tabId);

  // If vacuum-style, also read the page
  if (args.command === "ghost_vacuum" || args.limit) {
    const content = await readTabContent(tabId, args.limit || 30, args.selector);
    return { id: tab.id, url: tab.url, title: tab.title, content };
  }

  return { id: tab.id, url: tab.url, title: tab.title };
}

function waitForTabLoad(tabId, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(); // Don't fail on timeout, page may be usable
    }, timeout);

    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        // Small delay for JS to settle
        setTimeout(resolve, 200);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ---------------------------------------------------------------------------
// Page reading
// ---------------------------------------------------------------------------

async function readPage(args) {
  const tabId = await getActiveTabId(args);
  const content = await readTabContent(tabId, args.max_chars || 4000, args.selector);
  const tab = await chrome.tabs.get(tabId);
  return { url: tab.url, title: tab.title, content };
}

async function fetchPdf(args) {
  const tabId = await getActiveTabId(args);
  const tab = await chrome.tabs.get(tabId);
  const sourceUrl = new URL(tab.url || "");
  if (!['http:', 'https:'].includes(sourceUrl.protocol)) {
    throw new Error(`UNSUPPORTED_PDF_SOURCE: ${sourceUrl.protocol || 'unknown'} URLs are not supported`);
  }

  const uploadUrl = new URL(args.upload_url || "");
  const expectedPort = String(port + 1);
  const expectedPath = `/pdf-upload/${args.upload_token}`;
  if (
    uploadUrl.protocol !== "http:" ||
    uploadUrl.hostname !== "127.0.0.1" ||
    uploadUrl.port !== expectedPort ||
    uploadUrl.pathname !== expectedPath ||
    uploadUrl.search ||
    uploadUrl.hash
  ) {
    throw new Error("INVALID_UPLOAD_URL: PDF bytes may only be uploaded to the Ghost loopback bridge");
  }

  const maxBytes = Number(args.max_bytes) || 50 * 1024 * 1024;
  const response = await fetch(sourceUrl.href, {
    credentials: "include",
    cache: "no-store",
    redirect: "follow",
  });
  if (!response.ok) {
    throw new Error(`PDF_FETCH_FAILED: HTTP ${response.status} ${response.statusText}`);
  }
  const declaredLength = Number(response.headers.get("content-length")) || 0;
  if (declaredLength > maxBytes) {
    throw new Error(`PDF_TOO_LARGE: content-length ${declaredLength} exceeds ${maxBytes} bytes`);
  }

  const chunks = [];
  let byteLength = 0;
  const reader = response.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    byteLength += value.byteLength;
    if (byteLength > maxBytes) {
      await reader.cancel();
      throw new Error(`PDF_TOO_LARGE: download exceeds ${maxBytes} bytes`);
    }
    chunks.push(value);
  }

  const pdfBytes = new Uint8Array(byteLength);
  let offset = 0;
  for (const chunk of chunks) {
    pdfBytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let signatureOffset = 0;
  while (signatureOffset < pdfBytes.length && [9, 10, 12, 13, 32].includes(pdfBytes[signatureOffset])) {
    signatureOffset += 1;
  }
  const signature = String.fromCharCode(...pdfBytes.slice(signatureOffset, signatureOffset + 5));
  if (signature !== "%PDF-") {
    throw new Error("NOT_A_PDF: downloaded content does not have a PDF signature");
  }

  const upload = await fetch(uploadUrl.href, {
    method: "POST",
    headers: {
      "Content-Type": "application/pdf",
      "X-Ghost-PDF-Token": args.upload_token,
    },
    body: pdfBytes,
  });
  if (!upload.ok) {
    const detail = await upload.text();
    throw new Error(`PDF_UPLOAD_FAILED: HTTP ${upload.status} ${detail.slice(0, 300)}`);
  }

  return {
    id: tab.id,
    url: tab.url,
    title: tab.title,
    content_type: response.headers.get("content-type") || "application/pdf",
    bytes: byteLength,
  };
}

async function readTabContent(tabId, maxChars, selector) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (sel, max) => {
      const el = sel ? document.querySelector(sel) : document.body;
      if (!el) return { error: `Selector "${sel}" not found` };

      // Build a structured view of the page
      const items = [];
      let charCount = 0;

      function walk(node, depth) {
        if (charCount >= max) return;

        if (node.nodeType === Node.TEXT_NODE) {
          const text = node.textContent.trim();
          if (text) {
            items.push(text);
            charCount += text.length;
          }
          return;
        }

        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const tag = node.tagName.toLowerCase();

        // Skip hidden, scripts, styles
        if (["script", "style", "noscript", "svg"].includes(tag)) return;
        const style = window.getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden") return;

        // Clickable elements get numbered
        const clickable = tag === "a" || tag === "button" || tag === "input" ||
          tag === "select" || tag === "textarea" || node.getAttribute("role") === "button" ||
          node.getAttribute("onclick") || node.getAttribute("tabindex");

        if (clickable) {
          const label = node.textContent.trim().slice(0, 100) || node.getAttribute("aria-label") ||
            node.getAttribute("placeholder") || node.getAttribute("title") || tag;
          const href = node.getAttribute("href") || "";
          const type = node.getAttribute("type") || "";
          // Current form values can contain credentials or session material.
          // Enumerate the control, but never send its value to the agent.
          const value = (tag === "input" || tag === "textarea") && node.value
            ? "[REDACTED]"
            : "";

          // Store element reference for clicking
          node.setAttribute("data-ghost-id", items.length);

          let desc = `[${items.length}] `;
          if (tag === "a") desc += `link: ${label}` + (href ? ` (${href.slice(0, 80)})` : "");
          else if (tag === "input") desc += `input(${type}): ${value || label}`;
          else if (tag === "select") desc += `select: ${label}`;
          else if (tag === "textarea") desc += `textarea: ${value || label}`;
          else desc += `${tag}: ${label}`;

          items.push(desc);
          charCount += desc.length;
          return; // Don't recurse into clickable children
        }

        for (const child of node.childNodes) {
          if (charCount >= max) break;
          walk(child, depth + 1);
        }
      }

      walk(el, 0);
      return { text: items.join("\n"), length: items.length };
    },
    args: [selector || null, maxChars],
  });

  if (!results || !results[0]) throw new Error("Failed to read page");
  if (results[0].result?.error) throw new Error(results[0].result.error);
  return results[0].result?.text || "";
}

// ---------------------------------------------------------------------------
// Click
// ---------------------------------------------------------------------------

async function click(args) {
  const tabId = await getActiveTabId(args);
  const choice = args.choice;

  if (choice === undefined && !args.selector) throw new Error("Provide choice (number) or selector");

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (choice, selector) => {
      let el;
      if (selector) {
        el = document.querySelector(selector);
      } else {
        el = document.querySelector(`[data-ghost-id="${choice}"]`);
      }
      if (!el) return { error: `Element not found: ${selector || `choice ${choice}`}` };

      // Scroll into view
      el.scrollIntoView({ behavior: "instant", block: "center" });

      // Dispatch real events
      el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
      el.click();

      return {
        clicked: true,
        tag: el.tagName.toLowerCase(),
        text: (el.textContent || "").trim().slice(0, 100),
      };
    },
    args: [choice, args.selector || null],
  });

  if (!results || !results[0]) throw new Error("Click failed");
  if (results[0].result?.error) throw new Error(results[0].result.error);

  // Wait for potential navigation
  if (args.wait) {
    await new Promise(r => setTimeout(r, args.wait === "networkidle" ? 2000 : (parseInt(args.wait) || 1000)));
  }

  return results[0].result;
}

// ---------------------------------------------------------------------------
// Fill / Type
// ---------------------------------------------------------------------------

async function fill(args) {
  const tabId = await getActiveTabId(args);
  const { selector, choice, value } = args;

  if (!value && value !== "") throw new Error("value is required");

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (choice, selector, value) => {
      let el;
      if (selector) el = document.querySelector(selector);
      else if (choice !== undefined) el = document.querySelector(`[data-ghost-id="${choice}"]`);
      else el = document.activeElement;

      if (!el) return { error: "Element not found" };

      el.focus();
      el.value = value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));

      return { filled: true, tag: el.tagName.toLowerCase() };
    },
    args: [choice, selector || null, value],
  });

  if (!results || !results[0]) throw new Error("Fill failed");
  if (results[0].result?.error) throw new Error(results[0].result.error);
  return results[0].result;
}

// ---------------------------------------------------------------------------
// Keyboard
// ---------------------------------------------------------------------------

async function sendKey(args) {
  const tabId = await getActiveTabId(args);

  // If it's text to type, use a different approach
  if (args.text) {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (text) => {
        const el = document.activeElement || document.body;
        for (const char of text) {
          el.dispatchEvent(new KeyboardEvent("keydown", { key: char, bubbles: true }));
          el.dispatchEvent(new KeyboardEvent("keypress", { key: char, bubbles: true }));
          if (el.value !== undefined) el.value += char;
          el.dispatchEvent(new InputEvent("input", { data: char, inputType: "insertText", bubbles: true }));
          el.dispatchEvent(new KeyboardEvent("keyup", { key: char, bubbles: true }));
        }
      },
      args: [args.text],
    });
    return { typed: true, characters: args.text.length };
  }

  // Single key press
  const key = args.key;
  if (!key) throw new Error("Provide key or text");

  await chrome.scripting.executeScript({
    target: { tabId },
    func: (key) => {
      const el = document.activeElement || document.body;
      const opts = { key, bubbles: true, cancelable: true };

      // Map special keys
      if (key === "Enter") opts.code = "Enter";
      else if (key === "Escape") opts.code = "Escape";
      else if (key === "Tab") opts.code = "Tab";
      else if (key === "Backspace") opts.code = "Backspace";
      else if (key === "ArrowDown") opts.code = "ArrowDown";
      else if (key === "ArrowUp") opts.code = "ArrowUp";

      el.dispatchEvent(new KeyboardEvent("keydown", opts));
      el.dispatchEvent(new KeyboardEvent("keyup", opts));

      // Enter on forms should submit
      if (key === "Enter" && el.form) el.form.requestSubmit();
    },
    args: [key],
  });

  return { key, pressed: true };
}

// ---------------------------------------------------------------------------
// Eval — run arbitrary JS in page context
// ---------------------------------------------------------------------------

async function evaluate(args) {
  const tabId = await getActiveTabId(args);
  const script = args.script;
  if (!script) throw new Error("script is required");

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: (code) => {
      try {
        const fn = new Function(`return (${code})()`);
        const result = fn();
        return { value: result };
      } catch (err) {
        return { error: err.message };
      }
    },
    args: [script],
  });

  if (!results || !results[0]) throw new Error("Eval failed");
  if (results[0].result?.error) throw new Error(results[0].result.error);
  return results[0].result;
}

// ---------------------------------------------------------------------------
// Extract — structured extraction recipes
// ---------------------------------------------------------------------------

async function extract(args) {
  const tabId = await getActiveTabId(args);
  const recipe = args.recipe || "page_links";

  const recipes = {
    page_links: () => {
      return [...document.querySelectorAll("a[href]")].map(a => ({
        text: a.textContent.trim().slice(0, 100),
        href: a.href,
      })).filter(l => l.text && l.href.startsWith("http"));
    },
    page_images: () => {
      return [...document.querySelectorAll("img[src]")].map(img => ({
        src: img.src,
        alt: img.alt || "",
        width: img.naturalWidth,
        height: img.naturalHeight,
      }));
    },
    page_forms: () => {
      return [...document.querySelectorAll("form")].map(form => ({
        action: form.action,
        method: form.method,
        fields: [...form.elements].map(el => ({
          tag: el.tagName.toLowerCase(),
          type: el.type || "",
          name: el.name || "",
          id: el.id || "",
          placeholder: el.placeholder || "",
        })),
      }));
    },
    page_headings: () => {
      return [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")].map(h => ({
        level: parseInt(h.tagName[1]),
        text: h.textContent.trim(),
      }));
    },
    page_meta: () => {
      return {
        title: document.title,
        description: document.querySelector('meta[name="description"]')?.content || "",
        url: location.href,
        canonical: document.querySelector('link[rel="canonical"]')?.href || "",
        og: Object.fromEntries(
          [...document.querySelectorAll('meta[property^="og:"]')]
            .map(m => [m.getAttribute("property").slice(3), m.content])
        ),
      };
    },
  };

  const fn = recipes[recipe];
  if (!fn) throw new Error(`Unknown recipe: ${recipe}. Available: ${Object.keys(recipes).join(", ")}`);

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: fn,
  });

  if (!results || !results[0]) throw new Error("Extract failed");
  return { recipe, data: results[0].result };
}

// ---------------------------------------------------------------------------
// Screenshot
// ---------------------------------------------------------------------------

async function screenshot(args) {
  const tab = args.tab_id
    ? await chrome.tabs.get(args.tab_id)
    : (await chrome.tabs.query({ active: true, currentWindow: true }))[0];

  if (!tab) throw new Error("No tab to screenshot");

  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format: args.format || "png",
    quality: args.quality || 80,
  });

  return { dataUrl, width: tab.width, height: tab.height };
}

// ---------------------------------------------------------------------------
// Scroll
// ---------------------------------------------------------------------------

async function scroll(args) {
  const tabId = await getActiveTabId(args);
  const direction = args.direction || "down";
  const amount = args.amount || 500;

  await chrome.scripting.executeScript({
    target: { tabId },
    func: (dir, amt) => {
      const el = document.scrollingElement || document.documentElement;
      if (dir === "down") el.scrollTop += amt;
      else if (dir === "up") el.scrollTop -= amt;
      else if (dir === "top") el.scrollTop = 0;
      else if (dir === "bottom") el.scrollTop = el.scrollHeight;
      return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight };
    },
    args: [direction, amount],
  });

  return { scrolled: direction, amount };
}

// ---------------------------------------------------------------------------
// Wait
// ---------------------------------------------------------------------------

async function wait(args) {
  const tabId = await getActiveTabId(args);
  const selector = args.selector;
  const timeout = args.timeout || 10000;

  if (!selector) {
    await new Promise(r => setTimeout(r, args.ms || 1000));
    return { waited: args.ms || 1000 };
  }

  const start = Date.now();
  while (Date.now() - start < timeout) {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (sel) => !!document.querySelector(sel),
      args: [selector],
    });
    if (results?.[0]?.result) return { found: true, selector, elapsed: Date.now() - start };
    await new Promise(r => setTimeout(r, 200));
  }

  throw new Error(`Timeout waiting for "${selector}" after ${timeout}ms`);
}

// ---------------------------------------------------------------------------
// Presence overlay — show what an actor is working on, without editing
// ---------------------------------------------------------------------------

async function showPresence(args) {
  const tabId = await getActiveTabId(args);
  await chrome.scripting.executeScript({ target: { tabId }, files: ["overlay.js"] });
  const spec = {
    actor_id: args.actor_id,
    label: args.label,
    color: args.color,
    owner_color: args.owner_color,
    kind: args.kind === "human" ? "human" : "bot",
    status: args.status,
    ttl_ms: args.ttl_ms,
    choice: args.choice,
    selector: args.selector,
    text: args.text,
    rect: args.rect,
  };
  const [result] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (spec, clear) => {
      try {
        const overlay = globalThis.__ghostOverlay;
        if (clear) return { value: overlay.clear(spec.actor_id) };
        return { value: { ...overlay.show(spec), showing: overlay.list() } };
      } catch (err) {
        return { error: err.message };
      }
    },
    args: [spec, Boolean(args.clear)],
  });
  if (!result) throw new Error("Show failed");
  if (result.result?.error) throw new Error(result.result.error);
  return { tab_id: tabId, ...result.result.value };
}

// ---------------------------------------------------------------------------
// Message handler for popup and content scripts
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "get-status") {
    sendResponse(getStatus());
    return false;
  }
  if (msg.type === "connect") {
    port = msg.port || DEFAULT_PORT;
    token = (msg.token || "").trim();
    if (!token) {
      sendResponse({ ok: false, error: "Pairing token is required" });
      return false;
    }
    chrome.storage.local.set({ port, token }, () => {
      disconnect();
      intentionallyDisconnected = false;
      connect();
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.type === "disconnect") {
    disconnect({ forgetToken: true });
    sendResponse({ ok: true });
    return false;
  }
  return false;
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

chrome.storage.local.get(["port", "token"], (data) => {
  if (data.port) port = data.port;
  if (data.token) token = data.token;
  setBadge(token ? "OFF" : "PAIR", token ? "#ef4444" : "#f59e0b");
  connect();
});

// Keep service worker alive while connected
setInterval(() => {
  if (connected && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "heartbeat" }));
  }
}, 25000);
