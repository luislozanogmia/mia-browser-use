# Ghost multiplayer: several humans and bots in one browser

Design for letting humans and bots use Mia's in-app browser at the same time
through Ghost. The requirements (R1–R8) and acceptance scenarios come from the
Mia Multiplayer problem statement.

## What the code does today

Checked against mia-browser-use `b5c4a3a` and the Mia host in
`mia-wt-multiplayer/macos/src/browser.cjs` + `mia-ghost-bridge.cjs`.

| Claim in the problem statement | Verdict |
|---|---|
| 1. Most commands only reach the active tab | **True, and worse.** In Mia only `navigate`, `tab_switch` and `tab_close` read `tab_id` (`browser.cjs:1087` passes `tab_id` for `navigate` only). `vacuum` ignores it too. There is no `select` or `pdf_read` method in the in-app protocol. |
| 2. Focusing a tab steals the human's view | **True.** `tab_switch` activates the tab, hides the others (`setVisible`) and calls `webContents.focus()`. `tab_open` also activates the new tab. |
| 3. Screenshots only cover the visible tab | **True.** `capturePage()` runs on the active tab only. Whether it works on a hidden view is untested (see below). |
| 4. Numbered elements are shared state | **True.** Mia keeps one `tab.vacuumElements` array per tab, overwritten by every vacuum. Chrome writes `data-ghost-id` into the page DOM, which is the same problem. |
| 5. Input goes through focus | **Partly.** `click` and `fill` use DOM calls (`el.click()`, value setter + events) and don't take focus. `key` calls `webContents.focus()` and then `insertText`/`sendInputEvent`, so it steals focus. |
| 6. Nothing identifies who acted | **True.** One shared token, no actor field, no action events. |
| 7. One driver per bridge | **False for Mia.** Each socket connection carries one request and requests run in parallel with no queue. The problem is the opposite: nothing serializes calls on the same tab, and all of them share the tab's element list. The Chrome bridge also multiplexes by request id. |

Other findings:

- `wait` can run for 120 s but the socket times out after 35 s.
- A tab closed mid-call surfaces as a generic `BROWSER_ERROR` or `BROWSER_TIMEOUT`.
- Closing the last tab opens a new one.
- `frontend/multiplayer.html` is a scripted mock with no Ghost wiring.

## Who owns what

Ghost (this repo) owns the protocol, the clients (CLI, Hermes plugin), the
Chrome extension and bridge, and the mock host used in tests. The in-app host
(Mia's `browser.cjs`) owns the real behaviour behind every in-app method, so
most of R2, R4, R5 and R7 are host work described by the protocol here.

## Protocol v2

The host advertises it in `status`: `{"protocol": 2, "capabilities": [...]}`.
Clients fall back to v1 behaviour when it is missing.

### Actors (R1, R5)

Every request may carry `params.actor_id` (1–64 chars, `[A-Za-z0-9_.:-]`).
Ghost clients take it from `--actor` or `GHOST_ACTOR_ID`. A request with an
`actor_id` is an **actor call**:

- It must name its tab. Every method except `status`, `tab_list`, `tab_open`,
  `claims` and `subscribe` requires `tab_id`, otherwise `TAB_REQUIRED`. There
  is no fallback to the active tab.
- It never changes the human's view. `tab_open` opens the tab in the
  background. `tab_switch` returns `FORBIDDEN_FOR_ACTOR`.

Requests without `actor_id` keep v1 behaviour, so existing single-user setups
don't change.

### Per-actor element numbers (R3)

`vacuum` and `read` store their numbered elements under `(actor_id, tab_id)`.
`click`/`fill` with `choice` look only in the caller's own list. A number that
no longer resolves to the same element returns `STALE_ELEMENT` instead of
silently re-resolving. Each result carries a `snapshot` id, and `click`/`fill`
may pass it back to detect a list that was replaced.

`vacuum` accepts `tab_id` and an optional `url`. Without `url` it enumerates the
tab as it is.

### Background tabs (R2)

With a `tab_id`, `read`, `vacuum`, `click`, `fill`, `eval`, `scroll`, `wait`,
`screenshot`, `back`, `forward` and `reload` run on that tab's own
`webContents` without activating, showing or focusing it.

Screenshot of a hidden `WebContentsView`: Chromium stops painting hidden
views, so `capturePage()` can return an empty or stale image. The host should
call `webContents.incrementCapturerCount(size, stayHidden=true)` for the
capture (or while a bot works on the tab) and `decrementCapturerCount()` after.
This keeps the page rendering without showing it. This needs a real test in
Mia (acceptance scenario 4).

### Input without focus (R4)

Two input classes:

- **Focus-free** (default for actors): `click` (`el.click()`), `fill`
  (native value setter + `input`/`change`), and new `set_text`/`set_value`
  scoped to one element. These never touch `document.activeElement` or the
  human's caret.
- **Focus-taking**: `key` and real pointer events (`sendInputEvent`). On a tab
  where a human is present (the tab is showing, or had human input in the last
  few seconds) an actor call returns `HUMAN_ACTIVE`, and the bot decides whether
  to wait or use a focus-free path. On tabs no human is using it runs as today,
  but without `webContents.focus()` on the window.

Canvas editors (Google Docs, Sheets, Slides) ignore DOM edits and only react
to real input, so bots can't edit them safely while a human types in the same
document. For those, a bot should use the product's API or a tab of its own.
For Mia's own sheet, contract and deck surfaces, bots should edit through the
Mia Spaces op stream rather than simulating input.

### Claims (R6)

- `claim {tab_id, scope, ttl_ms, note}` → `{claim_id, expires_at}`. `scope` is
  `"tab"` or `{selector}`/`{range: "B2:B6"}`.
- `release {claim_id}`, and `claims {tab_id?}` lists live claims.

Claims are advisory. Action results include `conflicts: [...]` when the target
overlaps someone else's claim, and the host still runs the action. Claims
expire on their own (default 30 s, max 5 min). Human input always wins.

### Lifecycle (R7)

Typed errors:

- `TAB_NOT_FOUND`: the id never existed or was already gone.
- `TAB_CLOSED`: the tab closed during the call.
- `TAB_NAVIGATED`: the page changed under the call. The element list is
  dropped.
- `HUMAN_VIEWING`: an actor's `navigate`, `back`, `forward` or `reload` on a
  tab a human is viewing. Mia can ask the human and retry with a consent token.

The host must not open a replacement tab in response to an actor call.

### Events (R5)

`subscribe {filter?}` keeps its connection open. The host writes one framed
JSON event per action, using the same length-prefixed framing:

```json
{"event":"action","seq":42,"ts":1790000000000,"actor_id":"ledger","tab_id":3,
 "action":"fill","call_id":"a1b2","target":{"selector":"#b2","rect":{"x":10,"y":20,"w":80,"h":22}},
 "status":"started"}
```

`status` is `started`, `done` or `failed`, with `error` on failure. The host
also sends `claim`, `release`, `tab_closed` and `tab_navigated` events. The
host emits them because only it knows rects, tab lifecycle and human
presence. Mia's renderer subscribes in-process and needs no socket.

### Concurrency (R8)

The host runs calls on different tabs in parallel. On one tab it serializes
mutating calls (`click`, `fill`, `key`, `navigate`, `scroll`) and lets reads
overlap. Every call takes `timeout_ms` (default 30 s, capped below the
socket's idle timeout), and `wait` is capped to fit it.

## Chrome transport

- It gets `tab_id` on every command without activation (R1, most of R2).
- It gets `actor_id` tagging and per-actor element numbers (R3).
- It can't screenshot background tabs without `chrome.debugger`, which shows a
  banner. It can't detect human presence well.

So the full R2/R4 guarantees are in-app only.

## Plan

1. Ghost clients + protocol doc + mock host: `tab_id`/`actor_id` on every
   tool, v2 errors, per-actor elements, claims, events. Tests cover
   acceptance scenarios 2 and 5 against the mock.
2. Mia host (`browser.cjs`): implement v2 per this spec. Test scenarios 1, 3,
   4 and 6 in Electron.
3. Chrome extension: `tab_id` everywhere without activation, per-actor
   numbering.
