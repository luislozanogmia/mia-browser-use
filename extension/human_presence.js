/**
 * Wire the page's ask box to the extension.
 *
 * Injected into every open http(s) tab. Select text or crop an area, type a
 * question, and it goes to the bot for this tab; a reply on a card continues
 * the same thread. It never sends what the person types elsewhere on the page.
 */
(() => {
  if (globalThis.__ghostHumanPresence === globalThis.__ghostBuild) return;
  globalThis.__ghostHumanPresence = globalThis.__ghostBuild;

  // Called when the page is no longer shared; sharing it again injects a fresh copy.
  globalThis.__ghostHumanStop = () => {
    globalThis.__ghostHumanPresence = false;
  };

  // Select text, then ask this tab's bot about it.
  // Or crop an area of the page: the extension takes the picture.
  globalThis.__ghostOverlay?.enableAsk(({ text, question, target, image, thread, links }) => {
    try {
      chrome.runtime.sendMessage({ type: "ask", text, question, target, image, thread, links });
    } catch {
      // The extension was reloaded; this page's copy is orphaned.
    }
  }, rect => chrome.runtime.sendMessage({ type: "capture", rect }).then(reply => reply?.image || null));

  // Closing a conversation saves it to the reel. Stop (or closing it early) calls off its bot.
  if (globalThis.__ghostOverlay) {
    globalThis.__ghostOverlay.onClose = conversation => chrome.runtime.sendMessage({ type: "conversation", conversation });
    globalThis.__ghostOverlay.onCancel = id => chrome.runtime.sendMessage({ type: "ask_cancel", id });
  }
})();
