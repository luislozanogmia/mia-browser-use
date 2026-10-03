# Mia Browser Use contributor guide

Mia Browser Use has two supported browser targets:

1. the authenticated Chrome extension bridge; and
2. the authenticated Hermes Desktop browser endpoint.

Do not add another browser transport. Keep bridge listeners local, keep tokens
out of URLs and logs, bound all request/response bodies, and add negative auth
tests for changes to either transport.

## Local use

```bash
./install-extension.sh
./mia-browser-use status --backend chrome
./mia-browser-use call ghost_vacuum --args '{"url":"https://example.com","limit":30}'
```

Nothing is started by hand. The extension starts `mia-browser-use up` (ghost_up.py)
through the native messaging host (native_host.py) whenever it can't reach the
bridge; `up` runs the bridge and the local room relay, restarts
any that stop, and on first run creates a personal room. Its log is
`~/.ghost/bridge.log`. To change the room, edit `~/.ghost/bridge.json` or run
`mia-browser-use serve --room ...` once (it saves its settings), then stop `up`.

The token is created on first use and handed to the extension by the native
host. Direct HTTP callers must sign requests and verify response signatures
with it; normal users should use `mia-browser-use` instead.

Non-developers install with the Mac installer: `packaging/build-pkg.sh`.

`ghost_eval` is privileged and disabled by default. It requires explicit opt-in
in both the bridge/CLI and the Hermes plugin configuration.

Hermes Desktop integration follows `IN_APP_BROWSER_PROTOCOL.md`. The standalone
Hermes Agent adapter is in `hermes-plugin/` and must continue to validate with:

```bash
hermes plugins doctor --ci ./hermes-plugin
hermes plugins compat ./hermes-plugin
```

Run the repository checks with:

```bash
python3 -m compileall -q .
node --check extension/background.js
node --check extension/sidepanel.js
bash -n install-extension.sh
pytest -q
```
