# Mia Browser Use

Mia lets people and AI agents use the same signed-in browser together. It has
two parts:

- **The Mia Chrome extension.** A side panel where you chat with Mia, send bots
  to work on your tabs (they ask before anything that sends, posts or buys),
  ask about any text or area of a page and get the answer on the page, and keep
  a reel of what you did that becomes a PDF report.
- **The `mia-browser-use` command.** It gives any local AI agent (Claude Code,
  Codex, Hermes) a small, safe set of browser tools: read, open, click, fill,
  scroll, and more. It works with a signed-in Chrome window through the
  extension, or with the browser pane in Hermes Desktop.

Both connections are local and token-authenticated. Mia never asks the model
for browser credentials and has no command to export a browser session.

## Install

For everyday use, build the signed and notarized Mac installer with
`packaging/build-pkg.sh` and double-click `Mia-Browser-Use-<version>.pkg`.
The build requires the signing identities and notary profile described in the
script; `GHOST_DEV_UNSIGNED=1` makes a clearly labeled local test package.
The installer brings its own pinned Python and locked dependencies, starts
Mia by itself, and opens a page in Chrome that shows how to add the extension.

For development:

```bash
./install-extension.sh
```

Load `extension/` as an unpacked Chrome extension. The extension pairs itself
with the local bridge and starts it whenever it isn't running, so there's no
token to paste and nothing to start by hand. Logs go to `~/.ghost/bridge.log`.

```bash
./mia-browser-use status --backend chrome
./mia-browser-use call ghost_navigate --args '{"url":"https://example.com"}' --backend chrome
```

`ghost-cli` still works as an old name for `mia-browser-use`.

## Use Mia in Chrome

Click the Mia icon in the toolbar or press Alt+Shift+M to open the side panel.

- **Ask** reads the page and answers. **Do** turns your request into tasks for
  bots, one bot per tab, shown in the panel's Bots list. Anything that sends,
  posts, submits, buys or deletes waits for your approval.
- **Ask on the page:** select text, or crop an area with Alt+Shift+A. The answer
  appears as a card on the page, and you can reply to it. Page bots can search
  the web when the page doesn't say enough.
- **Immersive mode** keeps asking on: the pointer wears Mia's face, a click is
  still a normal click, and a drag crops.
- **Reel:** every page and answer is saved as a screenshot, in this browser
  only. **Make PDF** turns it into a report.
- **Rooms:** share a page with other people and their bots, and see each
  other's cursors and answers (`mia-browser-use room --help`).

Mia answers with your own Claude account through Claude Code. See the
[third-party service disclosure](TERMS.md) for how Mia installs and uses it.

## Hermes Desktop setup

Hermes Desktop must implement the authenticated local protocol in
`IN_APP_BROWSER_PROTOCOL.md`. Then either use the CLI directly:

```bash
./mia-browser-use status --backend hermes
```

or install `hermes-plugin/` as a Hermes Agent plugin. The plugin defaults to
`auto`, preferring the Hermes Desktop browser when present and otherwise using
the Chrome extension.

## Share the open page with the agent

`mia-browser-use context` prints a short note with the URL and title of the web page
the user has open, plus a reminder that the agent can read it with `mia-browser-use`. It
prints nothing when no browser is connected or the tab is not an `http(s)`
page, so it is safe to run on every prompt. Page content is never included.

Claude Code (`~/.claude/settings.json`) and Codex (`~/.codex/hooks.json`) can
add it to each message with a `UserPromptSubmit` hook:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "/path/to/mia-browser-use context --format hook", "timeout": 10}]}
    ]
  }
}
```

The Hermes plugin does the same through its `pre_llm_call` hook in local CLI,
TUI, and desktop sessions. Set `page_context: false` to turn it off.

## Supported tools

`ghost_status`, `ghost_tab_list`, `ghost_tab_open`, `ghost_tab_switch`,
`ghost_tab_close`, `ghost_navigate`, `ghost_vacuum`, `ghost_read`,
`ghost_pdf_read`, `ghost_click`, `ghost_fill`, `ghost_key`, `ghost_eval`, `ghost_screenshot`,
`ghost_scroll`, and `ghost_wait`. In Chrome, `ghost_show`, `ghost_suggest` and
`ghost_room` also let agents show what they're working on, suggest changes the
person accepts or rejects, and join rooms.

PDF reading is Chrome-only. The other listed commands are shared by Chrome and
Hermes Desktop.

`ghost_eval` remains available because some browser tasks require page-context
JavaScript, but it is disabled by default. Enable it explicitly with
`mia-browser-use serve --allow-eval` and `mia-browser-use call ... --allow-eval`, or set
`allow_eval: true` in the Hermes plugin. Eval can read page-visible secrets and
must be treated as privileged local code.

## Security

- All listeners bind only to loopback or a private Unix socket.
- Agent HTTP API requests and responses are signed with one-time,
  server-authenticated HMAC challenges.
- The extension and bridge mutually authenticate with nonce-bound HMAC proofs.
- The Chrome pairing token is never transmitted over the bridge, accepted in
  URLs, or written to logs. Hermes sends its separate token only inside its
  owner-only Unix socket.
- Password and token-like fields are redacted from ordinary page reads; fill and
  key results never echo entered text.
- Responses are bounded; PDF uploads additionally use one-time capabilities.
