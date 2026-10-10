# Play Automation integration review

This branch now integrates the Play Automation engine with GitHub main at
`fb2ebc9ce723e9e41a7cff5f600e15574c694b41`. The automation engine remains the
canonical implementation, while the current product shell and installers come
from main.

| Important functionality | This PR | Current main before integration | Decision |
|---|---|---|---|
| Creating complex automations | Plans outcomes, discovers real pages, tests incrementally, preserves validated steps and resumes from durable evidence. | Used a simpler builder with temporary test history and whole-script retesting. | Keep this PR's builder as the only builder. |
| Deciding an automation is ready | Separates rehearsal from live writes and requires two independent reviews of the exact candidate. | Could accept a rehearsal even when consequential steps were skipped. | Keep the PR completion gates and require live evidence for consequential steps. |
| Repeating work across lists | Keeps list navigation separate from detail pages and validates the complete loop, including pagination. | Focused on a representative item and could lose list state during detail navigation. | Keep the PR loop executor. |
| Recovering after uncertain writes | Journals builder actions, holds uncertain outcomes and reconciles supported visible records before continuing. | Had no equivalent durable builder recovery. | Keep bounded recovery and describe it only for supported observations. |
| Research and spreadsheet writes | Captures rendered text, verifies source URLs and written rows, handles rate limits and preserves literal cell values. | Had fewer extraction and write-verification safeguards. | Keep the PR safeguards. Reject copied values over 2,000 characters instead of silently truncating them, and report duplicate-key conflicts unless the existing row exactly matches. |
| Installing and connecting Mia | Previously carried the older development-focused install path. | Provides Mac, Windows and Linux installers, stable extension identity and guided helper setup. | Keep main's install, identity and onboarding stack. |
| Everyday browsing | Previously retained room/share/follow behavior. | Uses a single-user browser flow, supports on-page web research and renders clearer answer cards. | Keep main's single-user product flow and answer presentation. Do not restore multiplayer as the default. |
| Development reload | Adds an opt-in local extension watcher with bridge, task and panel idle checks. | Had no equivalent guarded watcher. | Keep it development-only and disabled by default. |

## Integrated safeguards

- External addresses chosen by the model still require approval unless the
  person supplied the complete URL. The task retains the source page origins
  and bounded page text used by this guard.
- A single-user action can take control of its tab without an extra control
  prompt, while send, submit, purchase, delete and other consequential actions
  retain their normal approvals.
- Saved Play spreadsheet writes treat multiple matching keys or a changed row
  as conflicts. An exactly matching row remains an idempotent no-write result.
- Copy reads request one character beyond the supported 2,000-character value
  limit and fail explicitly when the visible value is too long.

## Verification

- **667 Python tests passed.**
- **20 JavaScript tests passed.**
- Python compilation, extension JavaScript syntax, installer shell syntax and
  whitespace checks passed.
- The earlier company-research and GitHub-release workflows remain the bounded
  live evidence for the automation engine. This merge was verified locally; it
  was not installed and exercised on every supported operating system.

Private workflow journals and destination identifiers are intentionally not in
the PR.
