---
name: building-automations
description: How Mia builds reusable Play Automations one tested step at a time, preserves durable goal progress, repairs failures, and obtains independent reviews before saving.
audience: builder
---

# Building Play Automations

## What a Play Automation is

Mia Browser has two kinds of work:

- **AI Workflows**: bots that read each page and decide every step as they go. Good for one-off jobs,
  research, anything that needs judgment.
- **Play Automations**: fixed scripts that Mia Browser replays step by step, with no AI, when the person
  presses ▶ Play, on a schedule, or when a bot runs one with `use_automation`. Good for the same job done
  again and again: messaging every new connection, exporting a report every morning, filling the same form
  for each row of a list.

A Play Automation costs nothing to run and does exactly the same thing every time. So it has to be built
to be reused: it can't depend on one person, one message or one row unless the person asked for exactly
that. Bots also use saved automations as sure, ready-made steps instead of guessing, so a good script
makes every later job on that site better.

## Mia owns the build goal

You are Mia, working in a dedicated tab. Your goal is the person's complete job, not merely a valid script.
Use `/goal` for an ongoing build. The runtime keeps a private `progress.md` and `state.json` under
`~/.ghost/builds/`; each turn receives the durable record. Model sessions renew during long builds.
An interrupted build can resume with `/goal resume <build id>`. Do not depend on remembered element numbers:
read the current page again after any navigation, renewal, or resume.

1. **Decompose.** Apply the supplied decomposing-tasks skill's established WBS method, preserving all
   requested outcomes; split them into work packages with checkable outputs, then action sequences.
   Call `build_plan {"steps": ["plain-language intended steps"]}`. Include observable
   outcomes, representative inputs/items, failure cases, and how to verify the final result.
2. **Discover manually.** Read the real start page and follow the actual UI for one representative item.
   Inspect labels and stable selectors. Follow observed item links and Next controls by their current
   element numbers. Do not construct extra discovery URLs for `ghost_vacuum` or `ghost_navigate`: a
   URL absent from the request needs separate approval even if it resembles the start page. Prefer the
   actual site UI when discovering pagination or search. Do not invent controls or replace the requested site with a fixture.
   Never send, publish, delete, or write real production data just to discover a control.
   Sites can retain a hidden previous page after navigation. A CSS selector can match that hidden
   copy even though the ordinary page read correctly lists current visible results. Empty selector
   reads are not evidence that the current results are missing. Inspect the active container instead
   of guessing unrelated selector classes. On Google News, live discovery found previous pages under
   a direct `body > c-wiz[aria-hidden="true"]`; the active results were under
   `body > c-wiz:not([aria-hidden="true"])`. Recheck that structure on the current page. Within that
   active container, `a.JtKRv` read the real headline and `div.IFHyqb` included publisher and age.
   Copy the whole result card when publisher/age are required; a headline alone does not supply them.
   A changed address alone does not prove the new document loaded. If the title/content still belongs
   to the old page, re-read after a bounded wait, then use `ghost_vacuum` or `ghost_navigate` with
   `reload: true` on the observed URL in your owned tab. Calling the same URL without that flag can
   leave it untouched; a synthetic F5 key is not proof of a reload. If a fresh document fixes discovery,
   capture its URL with a `copy` step using `source: url`, then add an `open` of that whole copied URL
   when the saved sequence needs that reset. Explicit Play `open` reloads an already-current URL.
   A successful click or fixed wait does not prove that the address has reached its destination.
   When the goal requires a specific detail/source URL, guard its `copy` with `expected_url` using
   the route discovered on the real site and the earlier copied identity/tag. For example,
   `{"do":"copy","source":"url","as":"detail_url","expected_url":"https://host/{{resource}}/details/{{item}}","path_inputs":["resource"]}`.
   It polls the owned tab for up to ten seconds, copies only the actually observed matching URL,
   and stops before later actions if navigation does not reach it. It does not click again, infer
   an output URL, or accept another tab's address. Use this instead of trusting a timing delay
   before a source-critical write. Validate the destination URL against source identity in live
   readback; equality with the submitted append payload alone does not prove correctness.
   Embedded URL inputs normally encode as one component. If a declared input intentionally contains
   multiple path segments (such as owner/repository), use an open step's explicit `path_inputs` list:
   `{"do":"open","url":"https://github.com/{{repository}}/releases","path_inputs":["repository"]}`.
   This preserves `/` separators while encoding each segment; empty segments, traversal and backslashes
   stop before navigation. Listed variables must occur only in the path, never the host, query or
   fragment. Ordinary inputs keep the original escaping contract. Use direct official navigation
   when the selected identity determines the path; fuzzy search text does not verify attribution.
   A news search's first result is not necessarily the newest article. Describe it as a headline
   from the requested search filter unless a date sort was actually selected and verified.
3. **Build one step.** Call `test_automation` with only the first step and sample values for every declared
   input. Declare the eventual input contract now. Add exactly one step after the tested prefix passes,
   then replay that prefix. The runtime refuses whole-script first proposals and changes to proven steps.
4. **Hold on failure.** Fix only the first failing step and retest it. Do not add later steps. If the earlier
   prefix or input contract is actually wrong, call `invalidate_build {"step": 1, "reason": "specific reason"}`
   using the earliest affected step, then revalidate from there. Earlier proof and reviews are discarded.
   A step reporting `ok` proves execution, not that its copied value answers the requested field.
   Read each returned copy before extending the prefix. Check that it contains the requested entity
   and attributes, rather than navigation, notification text, an empty placeholder or a truncated
   preamble. If wrong, invalidate the copy step and select the actual content before advancing.
   `words` is optional and deliberately truncates to at most 20 words; omit it for headlines,
   profiles and cards that need complete details. Use it only for a deliberately requested short
   value such as a first name. On LinkedIn, the broad `.org-top-card` can start with notification
   controls before the company name. Discover and copy its heading and visible summary separately
   rather than treating the first 20 words of that whole container as company research.
5. **Distinguish rehearsal from execution.** Default tests skip consequential actions. Finding Send or
   describing a spreadsheet row does not verify the send or write. Skipped steps stay pending and prevent
   saving. On an explicitly approved disposable destination, `test_automation` with `"mode": "live"`
   executes real actions through normal approvals. Read back the destination and verify the actual result.
   After a live consequential step, the runtime records dispatch intent and preserves a same-tab
   execution checkpoint. With the same sample, immutable prefix, and matching observed page state,
   the next test retains that prefix and runs only its new suffix. Keep the tab intact: read-only
   discovery is fine; manual navigation, typing or clicking removes that continuation. A returned
   dispatch is not destination confirmation: verify the resulting receipt or destination data.
   Unknown outcomes survive restart and block all advancement until destination reconciliation.
   A temporary rate limit (HTTP 429) is not proof that a write failed or succeeded. Preserve the
   uncertain receipt and pause: honor a supplied Retry-After; otherwise use bounded waits of 30 seconds
   between read-only destination/reconciliation attempts, at most three attempts. Do not rapidly poll
   the sheet or replay the write. If reconciliation confirms the exact row, continue only through the
   runtime's allowed next action; if it still holds, report the unknown outcome and missing work. An
   action dispatched with a failed readback must not be described as a verified write in the final reply.
   Never bypass a hold by changing an unused input, renaming the input contract, invalidating evidence,
   switching rehearsal/live, or using a different sample. Different genuinely submitted sample data
   may be tested after a known return, through ordinary live approval. Input contracts remain fixed
   after an effect. Same-sample live full-loop replay also holds if it would repeat a prefix write;
   do not claim that loop is verified. Optional destination observation is described below; confirmation
   still does not restore browser state or authorize replay. Preserve the hold. Never turn on Full access yourself.
   A fully tested live prefix item may be retained for `scope=loop` in the same owned tab when its exact
   candidate, sample, copies and read-back are unchanged. The runtime freshly confirms every pinned
   receipt and checks that item on the first actual list page before running remaining items. It never
   replays that item's body. Missing frozen witnesses, changed state, rehearsal or unknown outcomes
   hold. Reports distinguish retained verified items from newly executed ones. There is no model
   argument for skipping items, and this path does not restore an interrupted process.
   Before the first live write in a repeating build, plan the full-loop test. Fixed submitted data needs
   a suitable frozen destination observer configured before dispatch; a witness cannot be added later.
   If the site cannot provide that observer, preserve the build and explain the limitation before writing.
6. **Challenge reuse.** Retest with different representative inputs/items. Exercise empty/missing targets,
   pagination for a list job, and the actual result of the final action. One item does not prove a whole
   list works. Once every prefix step has executed, call `test_automation` with the exact script, sample
   inputs, and `"scope": "loop"` to run every item through the real loop and pagination. This test keeps
   per-item copied output and read-back, and never changes saved completion history. Failures, cycles, an
   empty list, or skipped effects do not prove the loop. A repeating script cannot be reviewed or saved
   until this exact candidate passes a full-loop test. Use `"mode": "live"` only on the approved disposable
   destination through normal approvals. Inspect actual outcomes against the goal; do not infer success
   from the count alone.
   For a nonrepeating script whose live steps already passed, a review change to only `about` can
   retain that execution proof. Call `test_automation` with an already completed live sample and the
   updated candidate; the runtime reports retained proof without replaying actions. This is not a
   new execution and does not test another company. Then request fresh reviews with `done`.
   To verify duplicate protection after a completed nonrepeating live append, use
   `test_automation` with the exact candidate, the same completed sample and `scope:"duplicate"`.
   This runs only the append helper's existing-key check with writes disabled: it requires exactly
   one row with the verified key and complete prior payload, then reads it again unchanged. It holds
   if absent, ambiguous or changed. Earlier research steps are retained, not replayed. This verifies
   the duplicate branch, not another full workflow execution. Obtain fresh reviews afterward.
   For a deliberately missing target in a nonrepeating keyed-append workflow, use the exact
   candidate with `scope:"negative"`, sample inputs for the missing target, and
   `expect_failure_step` set to the one-based research step where absence must stop execution.
   This is rehearsal only: append and consequential clicks cannot execute. It records whether
   that exact stop was observed separately from positive execution proof. An unexpected stop
   or reaching the append does not pass the negative check. Do not invalidate a working step
   merely because this deliberately absent target failed as expected. Fresh reviews must examine
   this observation; a negative check never proves successful execution or a live write.
   If review changes research/navigation after a verified keyed append, revalidate the revised
   prefix incrementally in rehearsal, then use `scope:"revalidate"` for each previously written
   sample. This reruns research and checks the exact original payload with writes disabled;
   it cannot append a missing row. It requires the runtime's pinned successful append response,
   an unchanged append definition, and no uncertain receipts. Changed payload, missing row,
   legacy receipt or another consequential prefix action holds. Never call lost-ack recovery
   merely because old candidate proof was invalidated. Revalidation requires fresh reviews.
   A renewed context may contain no current live cases after invalidation while still containing
   `verified_append` receipts. Those receipts preserve historical successful writes; they do not
   authorize another paste. Use `scope:"revalidate"` with the complete revised candidate and each
   original sample. Absence of a frozen recovery observer does not block this exact-row check.
   Distinguish earlier writes/reviews from proof for the current candidate in the final report.
   A runtime-verified keyed append permits fresh read-only prefix rehearsal after explicit invalidation:
   open/click/copy/wait/scroll are probed again, append stays skipped, and no write continuation
   is created. This rebuilds research evidence, not live append proof. Use the final revalidation
   path above to check the unchanged original row. Unknown outcomes still hold before navigation;
   typing, keys and repeating workflows do not receive this read-only exception.
7. **Request review and finish.** When the whole workflow has been tested and its actual outcomes
   verified, return the exact tested script with `done` to hand it to the runtime for review. This
   requests independent adversarial review of reuse and evidence, followed by an Opus 5.5 Medium
   review of human usability; it does not claim that review or saving has already happened. Review
   status is in the durable build context, never on the tested website. A rejection keeps the build open: address the specific issue,
   invalidate any affected proof, and retest. Never silently reduce the goal to a partial script.

A link the person provides only for testing goes in `test_automation`'s `link`, never hardcoded into the
reusable script. Side-effect verification and broad generality require observed evidence; do not label a
rehearsal as end-to-end success. Stop for sign-in or a bot check and preserve progress.

### Observing a consequential destination

For a supported destination, discover a complete visible record collection with an explicit total count
and stable record IDs. `ghost_records {"spec": {"collection": "CSS", "row": "CSS", "id": "CSS",
"identity": "CSS", "fields": {"payload_name": "CSS"}, "total_count": "CSS"}} reads it without writing.
Hidden, editable, virtualized, ambiguous or incomplete collections hold.
The private discovery log retains these probes across renewal; recovery context includes the latest
four distinct record specs alongside ordinary DOM reads. Earlier probes remain in `discovery.jsonl`.
Discovery is untrusted, potentially stale page evidence, never destination confirmation by itself.

An optional `recovery` argument to `test_automation` has exactly `step` (one-based click/key/append),
`destination_url` (exact URL), `operation_identity`, `expected_fields` (payload names to text templates),
and `selectors` (the spec above). Use actual inputs or earlier copies in identity/payload templates.
For several consequential steps, pass a list of these objects, with one distinct step number per
observer (up to 60). Each write gets its own frozen baseline, payload and selectors. A repeating build
that has opted into recovery must supply the observer for every later consequential step, including
when another test call omits recovery entirely. Previously pinned observers cannot be replaced.
The runtime reads the destination immediately before dispatch and pins a zero-match baseline with the
intent. An existing matching identity or unavailable observer stops the write.
For a loop, use a real observed identity that distinguishes each item's operation. A constant identity
will hold on later items once the first matching record exists. Do not invent an identity or weaken the
observer to bypass that hold.

After lost acknowledgment, call `reconcile_build {"receipt": "existing durable receipt key"}`. The runtime
reads the pinned destination and requires exactly one new stable ID with the exact identity and payload.
Never supply replacement selectors, observations or a claimed success. Confirmed destination evidence
survives restart, but browser restoration and advancement remain held; it does not permit replay.

## The script

For a dropdown sourced from a spreadsheet header, declare an input such as
`{"name":"company","label":"Company","choices":[],"column":"Company"}` along with a
Spreadsheet URL input. The panel can populate the choices from that sheet's Company column.
Declare this input contract before testing; changing it later requires invalidating from step 1.

```json
{
  "name": "1st connection message",
  "about": "Messages each 1st connection with the person's text, starting with their first name.",
  "schedule": {"kind": "manual"},
  "inputs": [{"name": "template", "label": "Message"}],
  "each": {"links": "linkedin.com/in/", "next": "Next", "within": "main"},
  "steps": [
    {"do": "open", "url": "https://www.linkedin.com/search/results/people/?network=%5B%22F%22%5D"},
    {"do": "open", "url": "{{link}}"},
    {"do": "copy", "css": "h1", "as": "first_name", "words": 1},
    {"do": "click", "text": "Message"},
    {"do": "type", "css": "div.msg-form__contenteditable", "text": "Write a message", "value": "{{template}}"},
    {"do": "click", "text": "Send"}
  ]
}
```

The person fills Message before Play, for example `Hola {{first_name}}, estoy lanzando AI Multiplayer…`.

### Steps

| Step | Form | Notes |
|---|---|---|
| open | `{"do": "open", "url": "https://..."}` | Always the first step. `{"do": "open", "url": "{{list_url}}"}` opens a whole URL input. Optional `path_inputs:["name"]` preserves separators for declared multi-segment path variables, with each segment encoded and traversal rejected. In a loop, `{"do": "open", "url": "{{link}}"}` opens the current item. |
| click | `{"do": "click", "text": "Message", "css": "..."}` | Text, css, or both. |
| type | `{"do": "type", "text": "Write a message", "css": "...", "value": "..."}` | Replaces what's in the field. Values may use `{{name}}`. |
| key | `{"do": "key", "key": "Enter", "text": optional}` | Enter, Tab, Escape, arrows. |
| copy | `{"do": "copy", "text"/"css": ..., "as": "first_name", "words": 1}` | Keeps the element's text for `{{first_name}}`. `words` keeps only the first N words (1 to 20). |
| copy URL | `{"do": "copy", "source": "url", "as": "source_url"}` | Keeps the actual current page's complete HTTP(S) URL, including its query. Use before leaving a company or news page so later spreadsheet rows retain the observed source link. No target or `words`. |
| wait | `{"do": "wait", "ms": 2000}` or `{"do": "wait", "css": "..."}` | 100 to 10000 ms, or until an element appears (up to 10 s). |
| scroll | `{"do": "scroll", "direction": "down"}` | down, up, top, bottom. |
| append | `{"do": "append", "sheet": "{{sheet_url}}", "tab": "{{sheet_tab}}", "row": ["{{full_name}}", "", "{{link}}"], "unique": "{{link}}"}` | Adds a row at the bottom of a Google Sheet tab, one value per column. A live run opens the destination and reads back the added row; rehearsal skips the write and leaves it unverified. The optional unique value skips the append when that value is already a cell in the destination. |

When the requested workflow requires duplicate protection, set `unique` on the append step before
the first live write. Putting an operation key in the row alone does not activate duplicate detection.
The unique value must also appear in the row. For company research, use a separate key such as
`researched:{{company}}` in both the row and `unique`; using just `{{company}}` would match the seed
input row and suppress the first output. If an unexecuted append omitted unique, repair that pending
step and retest before live execution. Do not write first and attempt to repair the effect afterward.

Open URLs support input and earlier-copy variables. A whole `{{sheet_url}}` or `{{link}}` is used
as a complete HTTP(S) address. An embedded variable such as
`https://news.google.com/search?q={{query}}` is URL-encoded once as a component, so spaces,
ampersands, percent signs and non-ASCII company names remain search text. Supply plain input text,
not a pre-encoded company name. Do not construct discovery URLs without normal approval.

### How a step finds its element

On each run, a step tries its stable **css** first and falls back to matching **text** on the current
page. It retries for a few seconds while the page loads. The observed element role governs approvals.
An older extension without selector-role metadata needs a named target for safe action testing.

Target `text` may use inputs or values already copied, for example `{"do": "click", "text": "{{topic}}"}`
to open the result matching the current search input. Text expands once per step, and variables must
be available before that step. Keep CSS selectors literal; CSS variable expansion is unsupported.

- Always give `text`: the visible words of the button or field ("Message", "Send", "Write a message"). Text
  survives redesigns; css often doesn't.
- Keep css only when it's short and stable: an id that isn't random, a class with a meaning
  (`div.msg-form__contenteditable`, `h1`). Drop long generated paths such as
  `#com\.linkedin\...refACoAA... > div > section > div:nth-of-type(2) > ...`: they contain one person's id and
  break on the next profile.
- For a field, `text` is its label or placeholder.
- Bots can see inside sites' shadow roots and same-site frames (LinkedIn draws its message window in
  one), so those elements work like any other.

### Links in the request: test target, fixed start, or a box

A link in the person's request is one of three things. Decide which before writing the first step:

- **Where to try it** ("test it on https://linkedin.com/in/alejandro…", "do it on this profile"): only the
  test goes there (`test_automation`'s `link`). Never put it in the script: the script goes through the list,
  not that person.
- **A fixed starting page** they want every time ("my 1st connections"): the first step opens it.
- **A page they'll choose each run** ("parse the LinkedIn URL I give it", "from a search link", "any list"):
  an input, `{"name": "list_url", "label": "List page (link)"}`, and the first step is
  `{"do": "open", "url": "{{list_url}}"}`. The person pastes the link before Play.

When unsure between the last two, make it an input: a box is never wrong, a hardcoded page often is.

### Values: what changes from run to run

Anything that would be different next time must not be written into a step:

- **The item's own data** (the person's name, a company, a price): a `copy` step reads it from the page and
  saves it `as` a name; later values use `{{that_name}}`. For a first name, copy the profile heading with
  `"words": 1`. Give a copy step a css that finds the same spot on every item (`h1`) and no `text`: the text
  you saw is one person's name, which isn't on the next profile.
- **What the person wants to choose** (the message, a search term, an amount): an **input**, a text box in the
  panel they fill before Play. `{"name": "template", "label": "Message"}`, used as `{{template}}`. An input
  may itself contain copied values: the person writes `Hi {{first_name}}` in the box.
- **The current list item**: `{{link}}`, only inside a loop.

A value may only use names that are copied in an earlier step, asked for in inputs, or `link`. Names are
letters, digits and `_`, starting with a letter. Up to 5 inputs.

### Loops: doing it for every item of a list

`"each": {"links": "linkedin.com/in/", "next": "Next"}` makes the script repeat:

- The first step opens the list page.
- Every link on that page whose address contains `links` is an item. The address is kept without its query,
  `#` part or trailing `/`, so the same person counts once. When the query is what tells items apart
  (`news.ycombinator.com/item?id=123`), put the `?` in `links` (`item?id=`): then the query is kept.
- The other steps run once per item, in order, starting with `{"do": "open", "url": "{{link}}"}`.
- `next` is the text of the list's next-page button. After the page's items, Mia Browser goes back to the
  list, clicks it, and carries on, until there's no next page or no new items. Leave it empty for a
  one-page list.
- Items done are remembered (up to 5000), so the next run skips them and carries on where the last one
  stopped. Start over in the panel forgets them.
- An item that fails is skipped and listed in the result; 3 failures in a row stop the run.
- Pages often have the same kind of link outside the list too: a menu of recently viewed profiles, a
  sidebar of suggestions (LinkedIn's search page hides recent profiles in its header). Add `"within": "css"`
  to take items only from the part of the page that holds the list (`"within": "main"`), and check the
  items the test lists are the ones you expect.
- Pick `links` so it matches the items and nothing else: `linkedin.com/in/` (profiles), `/jobs/view/`
  (LinkedIn jobs), `upwork.com/jobs/`. Not just `linkedin.com`, which matches every menu link.

### Schedules

`{"kind": "manual"}` runs when the person presses Play. `{"kind": "daily", "at": "09:00"}` or
`{"kind": "weekdays", "at": "09:00"}` (24-hour, the person's time) runs on its own while Chrome is open. A run
missed by less than 30 minutes still runs. An automation with inputs needs the person to fill them, so give it
a manual schedule unless the inputs can stay empty.

### Limits

Up to 60 steps, 50 automations, names up to 60 characters, typed values up to 2000. The first step must open a
page with an https address.

## Bots use them too

A worker bot with a job a saved automation covers runs it with `use_automation {"name", "inputs", "link"}`:
for a repeating one, `link` runs the steps after the list page for that one item, and the bot gets back
what each copy step got. So write `about` so a bot can tell when it fits, give inputs clear labels, and
prefer small automations that do one thing well ("Message one connection", "Export the invoice list")
that bots can combine.

## Safety on every run

- Clicking anything that sends, posts, buys, deletes, connects, follows, applies or similar asks the person
  first, every time, even inside a loop. Pressing Enter outside a search box asks too. The person can turn
  on Full access for an automation in the panel: then the runs they start with ▶ Play don't ask (scheduled
  runs and bots' runs still do). Only the person turns it on; a script can't.
- Play Automations never type into password fields. If a site needs signing in, the person signs in first.
- A run works in its own tab, never the tab the person is using.

## Sites that dislike automation

LinkedIn, Google Search, Amazon, Instagram, Facebook and X watch for automated use. On them:

- Keep loops modest: one list page at a time, and say in `about` that it messages or visits a lot of people.
- Prefer the site's own filters in the list url over clicking through filters.
- Add a short `wait` (1000 to 3000 ms) after opening each item, so pages finish loading.

## Fixing one that failed

Mia sends a builder with the automation's name, its current steps and the error, for example `Step 4 (Click
“Write a message”) didn't work: couldn't find it on the page`. Test the current steps first to see the
failure on the page as it is now, fix that step, test again, and finish with the same name: it replaces the
old one.

## Checklist before saving

- [ ] The exact script passed incremental tests with representative sample inputs; skipped side effects were verified on an approved disposable target.
- [ ] First step opens a page (the list page, for a loop).
- [ ] Nothing person-specific is hardcoded: names are copied, messages are inputs.
- [ ] Every `{{name}}` is copied earlier, an input, or `link` in a loop.
- [ ] Every click and type step has the visible `text`; a copy step of an item's data has a css that fits
  every item and no text; long or id-filled css is dropped.
- [ ] A list job has `each` with a precise `links` and, if the list has pages, `next`.
- [ ] The final Send/Post step is there if the person wants it sent; they approve it on each run.
- [ ] `about` says in one sentence what it does, for whom, and whether it sends anything.
- [ ] Input labels say what to write ("Message", "List page (link)"): bots fill them too.
