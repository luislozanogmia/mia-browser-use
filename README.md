# Ghost CLI

Ghost gives local agents a small browser-control surface with two supported
targets:

- a signed-in Chrome window through the Ghost extension; and
- the browser pane already running in Hermes Desktop.

Both transports are local and token-authenticated. Ghost never asks the model
for browser credentials and provides no dedicated browser-session export command.

## Chrome setup

```bash
./install-extension.sh
```

Load `extension/` as an unpacked Chrome extension. The installer starts the
loopback bridge and prints a pairing token. Paste that token into the extension
popup once. It is stored in Chrome extension-local storage; the matching local
file is private to your OS user.

```bash
./ghost-cli status --backend chrome
./ghost-cli call ghost_navigate --args '{"url":"https://example.com"}' --backend chrome
```

## Hermes Desktop setup

Hermes Desktop must implement the authenticated local protocol in
`IN_APP_BROWSER_PROTOCOL.md`. Then either use the CLI directly:

```bash
./ghost-cli status --backend hermes
```

or install `hermes-plugin/` as a Hermes Agent plugin. The plugin defaults to
`auto`, preferring the Hermes Desktop browser when present and otherwise using
the Chrome extension.

## Share the open page with the agent

`ghost-cli context` prints a short note with the URL and title of the web page
the user has open, plus a reminder that the agent can read it with Ghost. It
prints nothing when no browser is connected or the tab is not an `http(s)`
page, so it is safe to run on every prompt. Page content is never included.

Claude Code (`~/.claude/settings.json`) and Codex (`~/.codex/hooks.json`) can
add it to each message with a `UserPromptSubmit` hook:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "/path/to/ghost-cli context --format hook", "timeout": 10}]}
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
`ghost_scroll`, and `ghost_wait`.

PDF reading is Chrome-only. The other listed commands are shared by Chrome and
Hermes Desktop.

`ghost_eval` remains available because some browser tasks require page-context
JavaScript, but it is disabled by default. Enable it explicitly with
`ghost-cli serve --allow-eval` and `ghost-cli call ... --allow-eval`, or set
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
