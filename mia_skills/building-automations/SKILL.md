---
name: building-automations
description: How a builder bot writes, tests and saves a Play Automation, the fixed script Mia Browser replays click by click with no AI. Builder bots always get this skill; Mia doesn't need it to plan a build.
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

## Who builds them

You do: a builder bot, in a tab of your own. Mia gives you the job, the page to start on, and the
automation's name, about and schedule. You look at the real pages, write the script, test it with
`test_automation`, fix it until every step works, and finish with it:

1. **Look.** Read the start page and the pages the job goes through. You may do the job by hand for one
   item (open, click, type) to see what each page shows, but never press the final Send, Post, Submit,
   Connect or Apply: that would be real. Each click or typed value you make comes back with "As a script
   step: {...}", the text and css Play would look for.
2. **Write** the script as below: what changes from run to run becomes a copy step, an input or a loop.
3. **Test** it: `test_automation {"automation": {...}, "inputs": {"template": "Hi {{first_name}}"}, "link":
   optional}`. It runs exactly as Play will, in your tab, except that steps that would ask the person
   (Send, Post, Enter outside a search box) are found and then skipped. It reports every step, what copy
   steps got, the items found on a list page, whether the next-page button is there, and for a failed step,
   the elements on the page instead. Give sample values to every input, so they're tested too.
4. **Fix and test again** until it says "Test passed". Typical fixes: the text the button really shows, a
   short css instead of text for a copy step, a `wait` before a step whose page was still loading, a more
   precise `links`.
5. **Finish** with `{"done": "what it does and what the person fills before Play", "automation": {...}}`, the
   exact script that passed. Anything else is sent back to you.

A link Mia says is "to try it on" goes in `test_automation`'s `link`, never in the script.

## The script

```json
{
  "name": "1st connection message",
  "about": "Messages each 1st connection with the person's text, starting with their first name.",
  "schedule": {"kind": "manual"},
  "inputs": [{"name": "template", "label": "Message"}],
  "each": {"links": "linkedin.com/in/", "next": "Next"},
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
| open | `{"do": "open", "url": "https://..."}` | Always the first step. `{"do": "open", "url": "{{list_url}}"}` opens a page the person gives in an input. In a loop, `{"do": "open", "url": "{{link}}"}` opens the current item. |
| click | `{"do": "click", "text": "Message", "css": "..."}` | Text, css, or both. |
| type | `{"do": "type", "text": "Write a message", "css": "...", "value": "..."}` | Replaces what's in the field. Values may use `{{name}}`. |
| key | `{"do": "key", "key": "Enter", "text": optional}` | Enter, Tab, Escape, arrows. |
| copy | `{"do": "copy", "text"/"css": ..., "as": "first_name", "words": 1}` | Keeps the element's text for `{{first_name}}`. `words` keeps only the first N words (1 to 20). |
| wait | `{"do": "wait", "ms": 2000}` or `{"do": "wait", "css": "..."}` | 100 to 10000 ms, or until an element appears (up to 10 s). |
| scroll | `{"do": "scroll", "direction": "down"}` | down, up, top, bottom. |

### How a step finds its element

On each run, a step looks first for an element whose **text** matches `text` on the page as it is now, and
only then for its **css**. It retries for a few seconds while the page loads.

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
  `#` part or trailing `/`, so the same person counts once.
- The other steps run once per item, in order, starting with `{"do": "open", "url": "{{link}}"}`.
- `next` is the text of the list's next-page button. After the page's items, Mia Browser goes back to the
  list, clicks it, and carries on, until there's no next page or no new items. Leave it empty for a
  one-page list.
- Items done are remembered (up to 5000), so the next run skips them and carries on where the last one
  stopped. Start over in the panel forgets them.
- An item that fails is skipped and listed in the result; 3 failures in a row stop the run.
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
  first, every time, even inside a loop. Pressing Enter outside a search box asks too.
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

- [ ] The last `test_automation` of this exact script passed, with sample inputs.
- [ ] First step opens a page (the list page, for a loop).
- [ ] Nothing person-specific is hardcoded: names are copied, messages are inputs.
- [ ] Every `{{name}}` is copied earlier, an input, or `link` in a loop.
- [ ] Every click and type step has the visible `text`; a copy step of an item's data has a css that fits
  every item and no text; long or id-filled css is dropped.
- [ ] A list job has `each` with a precise `links` and, if the list has pages, `next`.
- [ ] The final Send/Post step is there if the person wants it sent; they approve it on each run.
- [ ] `about` says in one sentence what it does, for whom, and whether it sends anything.
- [ ] Input labels say what to write ("Message", "List page (link)"): bots fill them too.
