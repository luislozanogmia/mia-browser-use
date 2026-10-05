# Ghost Chrome extension

The extension uses Chrome's native tab and scripting APIs. It connects only to
the Ghost loopback bridge and must authenticate before it can receive commands.

1. Start the bridge with `../mia-browser-use serve`.
2. Obtain the token with `../mia-browser-use bridge-token`.
3. Load this directory as an unpacked extension.
4. Click the Ghost icon, open settings (the gear) and paste the token and click **Connect**.

The extension stores the token in extension-local storage. The extension and bridge
exchange nonce-bound HMAC proofs, so the token is never transmitted or placed
in the connection URL. Disconnect removes the stored pairing token and does not
reconnect until the user pairs again.
