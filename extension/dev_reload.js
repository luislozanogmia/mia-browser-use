/* Opt-in development convenience. No remote code, production installs, or forced reloads. */
(function(root) {
  const FILES = Object.freeze([
    "manifest.json", "background.js", "dev_reload.js", "ghost_page.js", "canvas_text.js",
    "sidepanel.html", "sidepanel.js", "sidepanel.css", "reel_store.js",
  ]);
  class DevelopmentReloader {
    constructor({ chrome, fetch, crypto, idle, quiesce, now = Date.now }) {
      Object.assign(this, { chrome, fetch, crypto, idle, quiesce, now });
      this.available = false;
      this.enabled = false;
      this.baseline = null;
      this.candidate = null;
      this.stableSince = 0;
      this.checking = false;
      this.error = "";
    }
    async init() {
      try {
        this.available = (await this.chrome.management.getSelf()).installType === "development";
        this.enabled = this.available && (await this.chrome.storage.local.get("developmentReload")).developmentReload === true;
        // Snapshot even while disabled: enabling after an edit must detect the change.
        if (this.available) this.baseline = await this.fingerprint();
      } catch { this.available = false; this.enabled = false; }
    }
    async setEnabled(value) {
      if (!this.available || typeof value !== "boolean") return false;
      await this.chrome.storage.local.set({ developmentReload: value });
      this.enabled = value;
      return true;
    }
    status() { return { available: this.available, enabled: this.enabled, pending: Boolean(this.candidate), error: this.error }; }
    async fingerprint() {
      const hashes = [];
      for (const file of FILES) {
        const response = await this.fetch(this.chrome.runtime.getURL(file), { cache: "no-store" });
        if (!response.ok) throw new Error("Package file unavailable");
        const hash = await this.crypto.subtle.digest("SHA-256", await response.arrayBuffer());
        hashes.push([...new Uint8Array(hash)].map(v => v.toString(16).padStart(2, "0")).join(""));
      }
      return hashes.join(":");
    }
    async tick() {
      if (!this.available || !this.enabled || this.checking) return;
      this.checking = true;
      try {
        const hash = await this.fingerprint();
        this.error = "";
        if (!this.baseline) { this.baseline = hash; return; }
        if (hash === this.baseline) { this.candidate = null; return; }
        if (hash !== this.candidate) { this.candidate = hash; this.stableSince = this.now(); return; }
        if (this.now() - this.stableSince < 10000 || !this.idle()) return;
        const lease = await this.quiesce();
        if (!lease) return;
        let committed = false;
        try {
          // Every panel is frozen; final synchronous check closes the typing race.
          if (!this.enabled || !lease.valid() || !this.idle()) return;
          this.chrome.runtime.reload();
          committed = true;
        } finally { if (!committed) lease.release(); }
      } catch { this.candidate = null; this.error = "Could not read extension files; automatic reload is paused."; }
      finally { this.checking = false; }
    }
  }
  class PanelQuiescence {
    constructor({ nonce, timeout = 2000 }) { this.nonce = nonce; this.timeout = timeout; this.panels = new Set(); this.pending = null; }
    add(panel) { this.abort(); this.panels.add(panel); }
    remove(panel) { this.abort(); this.panels.delete(panel); }
    abort() { if (this.pending) this.pending.release(); }
    acknowledge(panel, message) {
      const pending = this.pending;
      if (!pending || message?.nonce !== pending.nonce || !pending.waiting.has(panel)) return;
      if (message.clean !== true) { pending.release(); return; }
      pending.waiting.delete(panel);
      if (!pending.waiting.size) pending.resolve(pending);
    }
    acquire() {
      if (this.pending || !this.panels.size) return Promise.resolve(null);
      return new Promise(resolve => {
        const panels = [...this.panels], nonce = this.nonce();
        const pending = { nonce, waiting: new Set(panels), resolve,
          valid: () => this.pending === pending && !pending.waiting.size,
          release: () => {
            if (this.pending !== pending) return;
            this.pending = null;
            clearTimeout(pending.timer);
            for (const panel of panels) { try { panel.postMessage({ type: "reload-abort", nonce }); } catch {} }
            resolve(null);
          },
        };
        this.pending = pending;
        pending.timer = setTimeout(pending.release, this.timeout);
        for (const panel of panels) { try { panel.postMessage({ type: "reload-quiesce", nonce }); } catch { pending.release(); break; } }
      });
    }
  }
  class PanelFreeze {
    constructor({ document, clean, ack, timeout = 5000 }) { Object.assign(this, { document, clean, ack, timeout }); this.nonce = null; }
    release(nonce = this.nonce) {
      if (nonce !== this.nonce || this.nonce === null) return;
      clearTimeout(this.timer);
      this.document.body.inert = this.wasInert;
      this.nonce = null;
      this.focus?.focus?.();
    }
    receive(message) {
      if (message?.type === "reload-abort") { this.release(message.nonce); return; }
      if (message?.type !== "reload-quiesce" || typeof message.nonce !== "string") return;
      if (this.nonce !== null || !this.clean()) { this.ack({ nonce: message.nonce, clean: false }); return; }
      this.nonce = message.nonce;
      this.wasInert = this.document.body.inert;
      this.focus = this.document.activeElement;
      this.document.body.inert = true;
      this.timer = setTimeout(() => this.release(), this.timeout);
      this.ack({ nonce: message.nonce, clean: true });
    }
  }
  root.MiaDevelopmentReload = { DevelopmentReloader, PanelQuiescence, PanelFreeze, FILES };
  if (typeof module !== "undefined") module.exports = root.MiaDevelopmentReload;
})(globalThis);
