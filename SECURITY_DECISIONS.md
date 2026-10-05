# Security decisions that stay

Notes for security reviews of Mia Browser Use. Hardening is welcome; removing
or breaking a feature to close a risk is not. When a finding touches one of
these, harden around the behavior described here instead of changing it, and
ask the product owner before anything that changes what a person can do.

## Mia uses the person's existing Claude Code

Mia runs the `claude` the person already has (on `PATH`, or the official
installer's `~/.local/bin/claude`). Only when it's missing does the installer
run Anthropic's official install script.

A review replaced this with a separate pinned copy in `~/.ghost/bin`, verified
by SHA-256, and ignored any existing install. That downloaded about 220 MB a
second time for people who already had Claude Code, and showed "Installing
Claude for Mia" to people who were already set up. It was reverted in 0.5.1.

Acceptable hardening keeps the existing install in use. For example: resolve
`claude` to an absolute path once, refuse a binary that is writable by other
users, or check that it reports a Claude Code version.

## The side panel has no Ask/Do switch: Mia decides

The panel sends a request without a mode, and Mia plans whether to read or to
act. What protects the person is approvals, not a mode: a bot asks before it
takes control of a tab, and before anything that sends, posts, buys, deletes
or opens a site with data from the page. An explicit `"mode": "ask"` still
limits a request to reading.

A review made a missing mode mean Ask. Bots could no longer click or type, so
Mia couldn't do anything. It was fixed in 0.5.1. Tighten the approvals if
needed, but keep this behavior.

## Nothing happens in Terminal

Mia is for people who don't use Terminal. Setup, sign-in, starting and
restarting all happen through Chrome and the side panel. A fix that asks a
person to open Terminal or run a command is not a fix for them.
