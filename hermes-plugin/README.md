# Ghost for Hermes Agent

This standalone plugin registers Ghost's browser tools in Hermes Agent. It can
control either:

- a regular Chrome window through the authenticated Ghost extension bridge; or
- the browser pane in Hermes Desktop through its authenticated local socket.

The default `backend: auto` tries Hermes Desktop first, then Chrome. Set
`backend` to `chrome` or `hermes` in the plugin configuration to require one.

## Install from a checkout

```bash
hermes plugins install ./hermes-plugin
hermes tools enable ghost
```

For Chrome, start `./mia-browser-use serve`, run `./mia-browser-use bridge-token`, and paste
that value into the extension's settings (the gear in its side panel). For Hermes Desktop, the app must expose
the protocol documented in `IN_APP_BROWSER_PROTOCOL.md` and create its private
token file.

The plugin has no dedicated browser-session export tool. JavaScript evaluation
is registered as `ghost_eval`, but is blocked by default because page JavaScript
can read page-visible secrets. Set `allow_eval: true` only when that capability
is required. The Chrome bridge must also be started with `--allow-eval`.
`ghost_pdf_read` is available only through the Chrome extension.

Before each turn in a local CLI, TUI, or desktop session, the plugin tells the
model which web page the user has open (URL and title only). Messaging gateway
sessions never receive it. Set `page_context: false` to turn this off.
