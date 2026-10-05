/**
 * Mia's chat in Chrome's side panel. It only talks to the background, which
 * talks to the bridge; the bridge runs the conversation and the workers. All
 * text from the bridge (and so from models and pages) is drawn as text nodes.
 */
const $ = id => document.getElementById(id);
const log = $("log"), input = $("input"), send = $("send"), status = $("status");
const STATUS_WORDS = { waiting: "waiting", working: "working…", needs_you: "needs you", done: "done", failed: "failed", stopped: "stopped" };
const prefs = { model: "claude-sonnet-5-5" };
const openAgents = new Set();  // agents whose tasks are shown
const tabTitles = new Map();
let state = null;
let connected = false;
let sending = false;

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) if (child !== "" && child != null) node.append(child);
  return node;
}

// A bot wears a Mote tinted to its color; Mia herself (no color) is the dot diamond.
function mote(color, extra = "") {
  const face = el("span", { className: `${color ? "mote" : "mote mia-mark"} ${extra}`.trim(), ariaHidden: "true" });
  if (color) face.style.setProperty("--hue", `${moteHue(color)}deg`);
  return face;
}

// The Mote art is pink (hue 337); this turns it to a bot's color.
function moteHue(hex) {
  let h = String(hex).replace("#", "");
  if (!/^[0-9a-fA-F]{3,8}$/.test(h)) return 0;
  if (h.length < 6) h = [...h.slice(0, 3)].map(c => c + c).join("");
  const [r, g, b] = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b), d = max - Math.min(r, g, b);
  if (!d) return 0;
  const hue = max === r ? ((g - b) / d + 6) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return Math.round(((hue * 60 - 337 + 540) % 360) - 180);
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

function setStatus(text, bad = false) {
  status.textContent = text;
  status.classList.toggle("bad", bad);
}

// -- talking to the background ----------------------------------------------------

function chat(action, extra = {}) {
  return new Promise(resolve => {
    // Never wait forever: say so when the background doesn't answer.
    const timer = setTimeout(() => {
      setStatus("Mia Browser is not running. Reload the extension on chrome://extensions or close and reopen Chrome", true);
      resolve({ ok: false });
    }, 15000);
    chrome.runtime.sendMessage({ type: "chat", action, ...extra }, reply => {
      clearTimeout(timer);
      const error = chrome.runtime.lastError?.message || (reply?.ok ? "" : reply?.error || "Mia Browser is not running. Reload the extension on chrome://extensions or close and reopen Chrome");
      if (error) setStatus(error, true);
      resolve(error ? { ok: false, error } : reply);
    });
  });
}

chrome.runtime.onMessage.addListener(msg => {
  if (msg.type === "chat-state" && msg.state && typeof msg.state === "object") {
    connected = true;
    render(msg.state);
  }
});

// -- drawing ------------------------------------------------------------------------

function render(next) {
  state = next;
  renderRoom(state.room);
  renderModels(state.models);
  renderLog(state.messages || []);
  renderTasks(state.tasks || [], state.agents || []);
  renderApprovals((state.tasks || []).filter(t => t.status === "needs_you"));
  renderClaude(state.claude);
  if (!$("history").hidden) renderHistory();
  if (connected && status.textContent.startsWith("Not connected")) setStatus("");
}

// Mia runs on the person's Claude account; offer to set it up when it isn't ready.
function renderClaude(claude) {
  const ready = !claude || !("installed" in claude) || (claude.installed && claude.signed_in);
  $("claudeBar").hidden = ready;
  if (ready) return;
  const busy = claude.busy;
  $("claudeText").textContent =
    busy === "installing" ? "Installing Claude for Mia. This takes about a minute…"
    : busy === "signing_in" ? "Sign in to Claude in the browser tab that just opened. Mia is ready right after."
    : "Mia answers with your Claude account. Sign in once and you're set.";
  $("claudeBtn").hidden = Boolean(busy);
  $("claudeBtn").textContent = claude.installed ? "Sign in to Claude" : "Set up Claude";
}

// What the room is called in this panel. Only a label: the room itself (what others join) keeps its id.
let roomNames = {};
let renaming = false;
const roomLabel = room => roomNames[room.name] || "Room 1";

function renderRoom(room) {
  if (renaming) return;  // the pill is a text box right now
  const pill = $("room"), faces = $("faces");
  pill.hidden = !room;
  faces.replaceChildren();
  if (!room) return;
  const people = room.members || [];
  // People and bots counted apart: "1 person · 2 bots", not "3 here".
  const humans = people.filter(m => m.kind !== "bot").length, bots = people.length - humans;
  pill.textContent = `${roomLabel(room)} · ${humans} ${humans === 1 ? "person" : "people"}` +
    (bots ? ` · ${bots} bot${bots === 1 ? "" : "s"}` : "");
  pill.title = `Room “${room.name}”. Click to rename it here.`;
  for (const m of people.slice(0, 6)) {
    const dot = el("span", { title: m.name });
    dot.style.background = /^#[0-9a-fA-F]{3,8}$/.test(m.color) ? m.color : "#B4B2A9";
    faces.append(dot);
  }
}

function renderModels(models) {
  const select = $("model");
  if (!Array.isArray(models) || select.options.length === models.length) return;
  select.replaceChildren(...models.map(m => el("option", { value: m.id, textContent: m.name })));
  select.value = models.some(m => m.id === prefs.model) ? prefs.model : models[0]?.id;
}

// Links in a message become clickable. Text stays text nodes; only http(s) addresses become links,
// and a link always shows its own address, so a page can't disguise where it goes.
const URL_RE = /\b(?:https?:\/\/|www\.)[^\s<>"']+/gi;

function linked(text) {
  const parts = [];
  let last = 0;
  for (const match of text.matchAll(URL_RE)) {
    let url = match[0].replace(/[.,;:!?)\]}'"»”’]+$/, "");
    // Keep a closing parenthesis that belongs to the address, as in Wikipedia links.
    if (match[0].charAt(url.length) === ")" && url.includes("(")) url += ")";
    const href = /^www\./i.test(url) ? `https://${url}` : url;
    let ok = false;
    try { ok = ["http:", "https:"].includes(new URL(href).protocol); } catch {}
    if (!ok) continue;
    parts.push(text.slice(last, match.index), el("a", { href, textContent: url, target: "_blank", rel: "noopener noreferrer" }));
    last = match.index + url.length;
  }
  parts.push(text.slice(last));
  return parts;
}

function renderLog(messages) {
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const nodes = messages.map(m => m.who === "you"
    ? el("div", { className: "you" }, ...linked(m.text))
    : el("div", { className: "mia" }, mote(m.color, "sm"), el("p", {}, ...linked(m.text))));
  // What each bot is doing shows in the Agents list below, not here.
  if (state.planning) nodes.push(el("div", { className: "mia thinking" }, mote("", "sm working"), el("p", { textContent: "Thinking…" })));
  $("hello").hidden = nodes.length > 0;
  log.replaceChildren($("hello"), ...nodes);
  if (atBottom || sending) log.scrollTop = log.scrollHeight;
}

const LIVE = ["waiting", "working", "needs_you"];

// One row per agent (one per tab): its face, the tab's name and what it's doing. Open it for its tasks.
function renderTasks(tasks, agents) {
  $("tasks").hidden = !tasks.length;
  if (!tasks.length) return;
  const byId = new Map((agents || []).map(a => [a.id, a]));
  const groups = new Map();
  for (const t of tasks) {
    const key = t.agent || t.id;
    if (!groups.has(key)) groups.set(key, { key, agent: byId.get(key) || { name: t.label, color: t.color, tab_id: t.tab_id, host: "" }, tasks: [] });
    groups.get(key).tasks.push(t);
  }
  const finished = tasks.filter(t => !LIVE.includes(t.status)).length;
  $("taskSum").textContent = `${groups.size} bot${groups.size === 1 ? "" : "s"} · ${finished} of ${tasks.length} finished`;
  $("stopAll").hidden = finished === tasks.length;
  const rows = [];
  for (const { key, agent, tasks: list } of groups.values()) {
    const now = list.find(t => t.status === "needs_you") || list.find(t => t.status === "working")
      || list.find(t => t.status === "waiting") || list[list.length - 1];
    const expanded = openAgents.has(key);
    const head = el("button", { className: `agent${expanded ? " open" : ""}`, ariaExpanded: String(expanded) },
      mote(agent.color, `sm${now.status === "working" ? " working" : ""}`),
      el("span", { className: "what" }, el("b", { textContent: tabName(agent) })),
      el("span", { className: `pill ${now.status}`, textContent: STATUS_WORDS[now.status] || now.status }),
      el("span", { className: "chev", textContent: "›", ariaHidden: "true" }));
    head.addEventListener("click", () => { expanded ? openAgents.delete(key) : openAgents.add(key); renderTasks(tasks, agents); });
    const live = list.some(t => LIVE.includes(t.status));
    const close = el("button", { className: "close", textContent: "×", title: live ? "Stop and close" : "Close",
                                 ariaLabel: `Close ${tabName(agent)}` });
    close.addEventListener("click", () => { openAgents.delete(key); chat("close", { agent: key }); });
    rows.push(el("div", { className: "agent-row" }, head, close));
    if (!expanded) continue;
    for (const t of list) {
      const isLive = LIVE.includes(t.status);
      const row = el("button", { className: `task sub ${t.status}`, title: t.tab_id ? "Show this tab" : "" },
        el("span", { className: "what" }, el("b", { textContent: t.title }),
          el("small", { textContent: (isLive ? t.note : t.result) || "" })),
        el("span", { className: `pill ${t.status}`, textContent: STATUS_WORDS[t.status] || t.status }));
      row.addEventListener("click", () => showTab(t.tab_id));
      if (isLive) {
        const stop = el("span", { className: "icon", role: "button", title: "Stop", ariaLabel: `Stop ${t.title}`, textContent: "×" });
        stop.style.marginLeft = "0";
        stop.addEventListener("click", event => { event.stopPropagation(); chat("stop", { task: t.id }); });
        row.append(stop);
      }
      rows.push(row);
    }
  }
  $("taskRows").replaceChildren(...rows);
}

// The tab's own title, looked up once; the site or the agent's name until then.
function tabName(agent) {
  const id = agent.tab_id;
  if (!Number.isInteger(id)) return agent.name;
  if (!tabTitles.has(id)) {
    tabTitles.set(id, "");
    chrome.tabs.get(id).then(tab => {
      tabTitles.set(id, tab.title || "");
      if (tab.title && state) renderTasks(state.tasks || [], state.agents || []);
    }).catch(() => {});
  }
  return tabTitles.get(id) || agent.host || agent.name;
}
chrome.tabs.onUpdated.addListener((id, info) => {
  if (info.title && tabTitles.has(id)) { tabTitles.set(id, info.title); if (state) renderTasks(state.tasks || [], state.agents || []); }
});

function renderApprovals(tasks) {
  $("approvals").replaceChildren(...tasks.map(t => {
    const yes = el("button", { className: "yes", textContent: "Approve" });
    const no = el("button", { textContent: "Reject" });
    const show = el("button", { className: "show", textContent: "Show me" });
    yes.addEventListener("click", () => { yes.disabled = no.disabled = true; chat("approve", { task: t.id }); });
    no.addEventListener("click", () => { yes.disabled = no.disabled = true; chat("reject", { task: t.id }); });
    show.addEventListener("click", () => showTab(t.tab_id));
    const card = el("div", { className: "approval" },
      el("div", { className: "head" }, mote(t.color, "sm"), el("span", { textContent: `${t.label} needs you` })),
      el("p", { textContent: t.question }), el("div", { className: "row" }, yes, no, show));
    card.style.setProperty("--c", t.color);
    return card;
  }));
}

async function showTab(tabId) {
  if (!Number.isInteger(tabId)) return;
  const tab = await chrome.tabs.update(tabId, { active: true }).catch(() => null);
  if (tab) chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
}

// -- the context chips: what Mia can see ----------------------------------------------

async function renderChips() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const chips = $("chips");
  chips.replaceChildren();
  if (!tab) return;
  const web = /^https?:/.test(tab.url || "");
  chips.append(el("span", { className: "chip", textContent: web ? `This tab · ${hostOf(tab.url)}` : "This tab can't be read",
    title: tab.title || "" }));
  if (!web) return;
  try {
    const [run] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => String(getSelection() || "").trim().slice(0, 80) });
    if (run?.result) chips.append(el("span", { className: "chip sel", textContent: `Selection · “${run.result}”` }));
  } catch {}
}

chrome.tabs.onActivated.addListener(renderChips);
chrome.tabs.onUpdated.addListener((id, info, tab) => { if (tab.active && (info.url || info.status === "complete")) renderChips(); });
window.addEventListener("focus", renderChips);

// -- the composer ------------------------------------------------------------------------

$("model").addEventListener("change", event => { prefs.model = event.target.value; chrome.storage.local.set({ chatPrefs: prefs }); });

async function submit() {
  const text = input.value.trim();
  if (!text || sending) return;
  sending = true;
  send.disabled = true;
  const reply = await chat("send", { text, model: prefs.model });
  if (reply.ok) {
    input.value = "";
  }
  sending = false;
  send.disabled = false;
  input.focus();
}

send.addEventListener("click", submit);
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  }
});
$("stopAll").addEventListener("click", () => chat("stop_all"));
// Click the room's name to rename it: Enter or clicking away saves, Esc cancels.
$("room").addEventListener("click", () => {
  const room = state?.room;
  if (!room || renaming) return;
  renaming = true;
  const pill = $("room");
  const box = el("input", { className: "room-pill", value: roomLabel(room), maxLength: 40, ariaLabel: "Room name" });
  let done = false;
  const finish = save => {
    if (done) return;
    done = true;
    const name = box.value.trim();
    if (save && name) {
      roomNames = { ...roomNames, [room.name]: name };
      chrome.storage.local.set({ roomNames });
    }
    box.replaceWith(pill);
    renaming = false;
    renderRoom(state?.room);
  };
  box.addEventListener("keydown", event => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") finish(false);
  });
  box.addEventListener("blur", () => finish(true));
  pill.replaceWith(box);
  box.select();
});
// The bots list starts collapsed: just the count until you open it.
$("tasksToggle").addEventListener("click", () => {
  const open = $("taskRows").hidden;
  $("taskRows").hidden = !open;
  $("tasksToggle").setAttribute("aria-expanded", String(open));
});
$("newChat").addEventListener("click", () => { toggleHistory(false); chat("new"); });

// -- past chats: saved on this computer by the bridge, so closing Chrome loses nothing ---------

function when(ts) {
  const d = new Date(ts), today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderHistory() {
  const chats = state?.chats || [];
  if (!chats.length) {
    $("chatList").replaceChildren(el("p", { className: "empty", textContent: connected ? "No past chats yet." : "Connect to Mia Browser to see past chats." }));
    return;
  }
  $("chatList").replaceChildren(...chats.map(c => {
    const open = el("button", { className: "chat-row" + (c.id === state.chat ? " current" : "") },
      el("span", { className: "chat-title", textContent: c.title || "Chat" }),
      el("small", { textContent: c.id === state.chat ? "Open now" : when(c.ts) }));
    open.addEventListener("click", () => { toggleHistory(false); if (c.id !== state.chat) chat("open_chat", { chat: c.id }); });
    const del = el("button", { className: "icon", title: "Delete", ariaLabel: `Delete ${c.title || "chat"}` });
    del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    del.addEventListener("click", () => chat("delete_chat", { chat: c.id }));
    return el("div", { className: "chat-item" }, open, del);
  }));
}

function toggleHistory(open) {
  if (open) toggleSettings(false);
  document.body.classList.toggle("in-history", open);
  $("history").hidden = !open;
  $("openHistory").setAttribute("aria-expanded", String(open));
  if (open) { renderHistory(); chat("sync"); }
}
$("openHistory").addEventListener("click", () => toggleHistory($("history").hidden));
$("closeHistory").addEventListener("click", () => toggleHistory(false));
$("claudeBtn").addEventListener("click", () => { $("claudeBtn").disabled = true; chat("claude_setup").finally(() => { $("claudeBtn").disabled = false; }); });

// -- settings: what the toolbar popup used to hold --------------------------------------

function toggleSettings(open) {
  if (open) toggleHistory(false);
  document.body.classList.toggle("in-settings", open);
  $("settings").hidden = !open;
  $("openSettings").setAttribute("aria-expanded", String(open));
  if (open) refreshSettings();
  else input.focus();
}

function updateSettings(info) {
  const on = Boolean(info.connected);
  connected = on;
  $("conn").classList.toggle("on", on);
  $("conn").title = on ? "Connected to Mia Browser" : "Not connected";
  $("dot").classList.toggle("on", on);
  $("statusLabel").textContent = on ? "Connected" : "Disconnected";
  $("statusDetail").textContent = on ? `Bridge on port ${info.port}`
    : info.paired ? "Starting Mia Browser on this computer…" : "Run the Mia Browser installer once, and it starts by itself after that";
  if (document.activeElement !== $("port")) $("port").value = info.port;
  $("version").textContent = `Mia v${info.version}`;
  // Pairing is automatic; the manual fields only matter when it hasn't worked.
  $("setup").hidden = on;
  $("disconnectBtn").hidden = !on;
  $("roomBox").hidden = !(on && info.room);
  $("followMe").checked = Boolean(info.follow);
  $("reelMode").checked = Boolean(info.reel);
  $("immersive").checked = Boolean(info.modes?.immersive);
  $("skipPrompt").checked = Boolean(info.modes?.skip);
  if (document.activeElement !== $("language")) $("language").value = info.language || "English";
  if (info.room) {
    const count = info.room.shared.length;
    $("roomLabel").textContent = `In a room as ${info.room.me.name || info.room.me.id} · ${count} shared page${count === 1 ? "" : "s"}`;
    const invites = $("roomInvites");
    invites.replaceChildren();
    const accepted = new Set(info.room.accepted || []);
    for (const page of info.room.pages || []) {
      if (!page?.url || accepted.has(page.url)) continue;
      const row = el("div", { className: "room-invite" });
      row.append(el("strong", { textContent: page.title || hostOf(page.origin) || "Shared site" }),
                 el("small", { textContent: page.href || page.origin }));
      const actions = el("div", { className: "room-invite-actions" });
      for (const [label, mode] of [...(page.href ? [["Open page", "new"]] : []), ["Use current tab", "current"]]) {
        const button = el("button", { type: "button", textContent: label });
        button.addEventListener("click", () => setting({ type: "accept-shared-page", url: page.url, mode }, 500));
        actions.append(button);
      }
      row.append(actions);
      invites.append(row);
    }
    invites.hidden = !invites.childElementCount;
  }
  // Offer only what applies to this tab.
  $("shareBtn").hidden = Boolean(info.tab_shared);
  $("unshareBtn").hidden = !info.tab_shared;
  if (!on) setStatus("Mia Browser is not running. Reload the extension on chrome://extensions or close and reopen Chrome", true);
  else if ($("status").textContent.startsWith("Not connected")) setStatus("");
}

function refreshSettings() {
  chrome.runtime.sendMessage({ type: "get-status" }, info => {
    if (!chrome.runtime.lastError && info) updateSettings(info);
  });
}

function setting(message, delay = 300) {
  $("error").textContent = "";
  chrome.runtime.sendMessage(message, result => {
    if ((message.type.endsWith("share-tab") || message.type === "accept-shared-page") && !result?.ok) {
      $("error").textContent = result?.error || "Could not reach the room";
    }
    setTimeout(refreshSettings, delay);
  });
}

$("openSettings").addEventListener("click", () => toggleSettings($("settings").hidden));
$("closeSettings").addEventListener("click", () => toggleSettings(false));
$("shareBtn").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return;
  const shareLink = $("shareFullLink").checked;
  if (shareLink && !confirm(`Share this full address with everyone in the room?\n\n${tab.url}\n\nIts path or search terms may contain private information.`)) return;
  setting({ type: "share-tab", share_link: shareLink }, 500);
});
$("unshareBtn").addEventListener("click", () => setting({ type: "unshare-tab" }, 500));
$("followMe").addEventListener("change", () => {
  if ($("followMe").checked && !confirm("Follow mode shares each active site's name with the room. Page paths and search terms stay private. Continue?")) {
    $("followMe").checked = false;
    return;
  }
  setting({ type: "follow", on: $("followMe").checked }, 500);
});
$("reelMode").addEventListener("change", () => setting({ type: "reel", on: $("reelMode").checked }));
for (const id of ["immersive", "skipPrompt"]) {
  $(id).addEventListener("change", () => setting({ type: "modes", immersive: $("immersive").checked, skip: $("skipPrompt").checked }));
}
$("language").addEventListener("change", () => setting({ type: "language", language: $("language").value }));
$("openReel").addEventListener("click", () => chrome.runtime.sendMessage({ type: "open-reel" }));
$("disconnectBtn").addEventListener("click", () => setting({ type: "disconnect" }, 200));
$("connectBtn").addEventListener("click", () => {
  const port = parseInt($("port").value, 10) || 9377;
  const token = $("token").value.trim();
  $("error").textContent = "";
  chrome.runtime.sendMessage({ type: "connect", port, token }, result => {
    if (!result?.ok) $("error").textContent = result?.error || "Connection failed";
    $("token").value = "";
    setTimeout(() => { refreshSettings(); if (connected) chat("sync"); }, 500);
  });
});
// The share button and the connection dot follow the tab and the bridge.
chrome.tabs.onActivated.addListener(refreshSettings);
setInterval(refreshSettings, 5000);

// -- start --------------------------------------------------------------------------------

(async () => {
  const stored = await chrome.storage.local.get(["chatPrefs", "roomNames"]);
  roomNames = stored.roomNames && typeof stored.roomNames === "object" ? stored.roomNames : {};
  const saved = stored.chatPrefs || {};
  if (saved.model) prefs.model = String(saved.model);
  renderChips();
  chrome.runtime.sendMessage({ type: "chat-last" }, reply => {
    if (chrome.runtime.lastError || !reply) return;
    connected = Boolean(reply.connected);
    if (reply.state) render(reply.state);
    if (connected) chat("sync");
    else toggleSettings(true);
  });
  refreshSettings();
  input.focus();
})();
