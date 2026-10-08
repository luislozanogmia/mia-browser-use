# Play Automation: proposed PR versus current main

Compared the local implementation based on `5e23a30` with current GitHub main
`fb2ebc9ce723e9e41a7cff5f600e15574c694b41` on October 8, 2026.

Recommendation: use this PR's automation engine as the integration foundation,
while retaining main's single-user experience and cross-platform installation.
Do not replace current main wholesale. This PR preserves the developed version
for review; it is not an integrated release.

| Important functionality | My PR | Current main | Recommendation |
|---|---|---|---|
| Creating complex automations | Plans the outcome, discovers pages, tests incrementally, remembers progress and repairs failed steps. | Simpler builder with temporary test history and whole-script retesting. | Keep the PR builder. Retire the old builder after integration. |
| Deciding an automation is ready | Separates rehearsal from actual writes, preserves evidence, requires two independent reviews of the exact candidate. | Can mark a rehearsal successful even when consequential steps were skipped. | Keep the PR completion gates. Remove skipped-action success as a save qualification. |
| Repeating work across lists | Preserves the list while opening detail pages separately; requires full-loop validation. | Tests one item; detail navigation and list reload can lose pagination state. | Keep the PR loop engine. Retire the old loop implementation. Unit coverage is stronger than the live evidence for arbitrary lists. |
| Repairing after a write or lost response | Records builder action intent, holds uncertain outcomes, reconciles supported visible records and revalidates known spreadsheet rows without rewriting. | No equivalent durable builder recovery. | Keep PR recovery. Extend it to normal saved Play before promising universal exactly-once sends/posts. |
| Capturing research and writing spreadsheets | Better visible-text capture, source URL checks, reusable inputs, literal cell preservation and rate-limit handling. | Fewer extraction and write-verification safeguards. | Keep PR safeguards. Fix silent long-text truncation and report same-key payload conflicts before making broad completeness claims. |
| Installing and connecting Mia | Older Mac/development installation and more exposed connection settings. | Mac, Windows and Linux installation, fixed extension identity, guided helper setup. | Keep main's installers, stable identity and onboarding. Keep advanced settings out of normal setup. |
| Everyday browsing and on-page research | Retains room/share/follow controls; on-page room bots cannot research the web. | Single-user browsing without a sharing step; on-page bots can research when asked. | Keep main's everyday experience. Keep multiplayer separate and optional if it remains a product requirement. |
| Readable answers and development updates | Older answer-card formatting; adds opt-in reload of local extension edits when idle. | Better formatted answer cards; no equivalent guarded development watcher. | Keep main's answer cards and the PR's development-only reload. Never enable the watcher for production/store installs. |

## Evidence and limits

- Current local checks: **662 Python tests passed**, **19 JavaScript tests
  passed**, plus Python compilation, JavaScript syntax, installer shell syntax
  and whitespace checks. These checks do not prove cross-platform installation.
- Two bounded fresh workflows were tested through the real native builder:
  company research and GitHub release tracking. They used one complete request,
  native corrections and ordinary product approvals. Their final candidates
  passed both reviews, saved Play worked on a third sample, and a full repeat
  left the destination CSV unchanged. Broader workflow reliability remains
  unproven. Private test journals and destination identifiers are not published.
- Long-text capture still stores at most 2,000 characters and can silently
  truncate. An isolated runtime probe confirmed 2,100 observed characters became
  2,000 stored characters without an error. The tested release notes fit under
  the limit; this is not proof of complete long-document capture.
- Normal saved Play skips an existing spreadsheet key without requiring the
  entire row to match or exactly one matching row. Strict payload comparison is
  available for builder duplicate/revalidation checks, not all normal runs.
- Durable action recovery is a builder capability for supported observations.
  It does not establish exactly-once behavior for all saved or scheduled actions.

## Source basis

These conclusions were checked against execution paths, not just descriptions:

- `automation_build.py`, `automation_recovery.py`, and `ghost_chat.py`: journal,
  live/rehearsal gates, reviews, recovery, full-loop execution and saved Play.
- `automations.py`, `extension/ghost_page.js`, and `extension/background.js`:
  input expansion, visible capture, cell writes, duplicate checks and retries.
- `extension/dev_reload.js` and side-panel files: opt-in local reload with
  bridge/task/panel idle checks.
- Main's `native_host.py`, installer sources and manifest: cross-platform
  startup and stable extension identity.
- Both versions' `ask_bot.py`, `extension/overlay.js`, `ghost_room.py`,
  `room_link.py`, and sharing paths: answer formatting and solo/room behavior.

## Integration order

1. Preserve this PR as the automation foundation and main as the product shell.
2. Integrate the builder, executor, evidence/recovery and spreadsheet changes
   into main's single-user interface and cross-platform runtime. Resolve shared
   code deliberately; do not keep two competing builders.
3. Add explicit long-text overflow handling and strict saved-Play duplicate
   conflict reporting. Keep recovery claims bounded until saved-run journaling
   is implemented and tested.
4. Run integrated checks and fresh browser tests again, including installation
   on the supported operating systems, before merging or releasing.
