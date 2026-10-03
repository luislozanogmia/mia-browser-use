/**
 * Report where the local human is working on a shared page.
 *
 * Injected only into tabs whose page the room shares. It sends the focused
 * element and the pointer position, each as a portable anchor (selector, text,
 * page rect) plus the pointer's offset inside its element. That way other
 * people's browsers put the cursor on the same element even at another window
 * size. It never sends what the human types, except a question they choose to
 * ask the room about text they selected.
 */
(() => {
  if (globalThis.__ghostHumanPresence === globalThis.__ghostBuild) return;
  globalThis.__ghostHumanStop?.(); // an older copy's tracker
  globalThis.__ghostHumanPresence = globalThis.__ghostBuild;

  const SEND_EVERY_MS = 120;
  const IDLE_AFTER_MS = 30000;
  let pending = null;
  let timer = 0;
  let lastPointer = null;
  let lastActive = Date.now();
  let idleTimer = 0;

  function send(status = "working") {
    timer = 0;
    const page = globalThis.__ghostPage;
    if (!page) return;
    const message = {
      type: "human_presence",
      status,
      focus: page.humanFocus(),
      pointer: lastPointer,
    };
    try {
      chrome.runtime.sendMessage(message);
    } catch {
      // The extension was reloaded; this page's tracker is orphaned.
      stop();
    }
    pending = null;
  }

  function queue() {
    lastActive = Date.now();
    pending = true;
    if (!timer) timer = setTimeout(send, SEND_EVERY_MS);
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => send("idle"), IDLE_AFTER_MS);
  }

  function onPointer(event) {
    const target = event.target instanceof Element ? event.target : null;
    if (!target || target.closest("ghost-overlay")) return;
    const r = target.getBoundingClientRect();
    if (!r.width || !r.height) return;
    lastPointer = {
      anchor: globalThis.__ghostPage.anchorOf(target),
      fx: (event.clientX - r.left) / r.width,
      fy: (event.clientY - r.top) / r.height,
    };
    queue();
  }

  const listeners = [
    ["pointermove", onPointer, { passive: true, capture: true }],
    ["focusin", queue, true],
    ["selectionchange", queue, false],
    ["scroll", queue, { passive: true, capture: true }],
  ];
  for (const [type, fn, opts] of listeners) document.addEventListener(type, fn, opts);

  function stop() {
    for (const [type, fn, opts] of listeners) document.removeEventListener(type, fn, opts);
    clearTimeout(timer);
    clearTimeout(idleTimer);
  }

  // Called when the page stops being shared; sharing it again injects a fresh tracker.
  globalThis.__ghostHumanStop = () => {
    stop();
    globalThis.__ghostHumanPresence = false;
  };

  // Select text, then ask the room's bots about it.
  // Or crop an area of the page: the extension takes the picture.
  globalThis.__ghostOverlay?.enableAsk(({ text, question, target, image, thread, links }) => {
    try {
      chrome.runtime.sendMessage({ type: "ask", text, question, target, image, thread, links });
    } catch {
      stop();
    }
  }, rect => chrome.runtime.sendMessage({ type: "capture", rect }).then(reply => reply?.image || null));

  // Closing a conversation saves it to the reel. Stop (or closing it early) calls off its bot.
  if (globalThis.__ghostOverlay) {
    globalThis.__ghostOverlay.onClose = conversation => chrome.runtime.sendMessage({ type: "conversation", conversation });
    globalThis.__ghostOverlay.onCancel = id => chrome.runtime.sendMessage({ type: "ask_cancel", id });
  }

  queue();
})();
