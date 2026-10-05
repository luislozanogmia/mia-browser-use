---
name: building-automations
description: How to make, save, fix and run Play Automations, the fixed scripts Mia Browser replays click by click with no AI. Load it whenever the person asks to make, save, schedule, change or repeat an automation, script or routine, or to do the same job for many people or items.
---

# Building Play Automations

## What a Play Automation is

Mia Browser has two kinds of work:

- **AI Workflows**: your bots in the chat. They read each page and decide every step as they go. Good for
  one-off jobs, research, anything that needs judgment.
- **Play Automations**: fixed scripts that Mia Browser replays step by step, with no AI, when the person
  presses ▶ Play or on a schedule. Good for the same job done again and again: messaging every new
  connection, exporting a report every morning, filling the same form for each row of a list.

A Play Automation costs nothing to run and does exactly the same thing every time. So it has to be built
to be reused: it can't depend on one person, one message or one row unless the person asked for exactly
that.

## How one gets made

1. **Do the job once while it's recorded.** Plan one task of kind `do` with `save_as`:
   `{"name": "2 to 5 words", "about": "one sentence", "schedule": {"kind": "manual"}}`.
   Its bot does the job for real while every open, click, typed value and key press is recorded with the
   element's text and selector.
2. **Mia makes it reusable.** When the bot finishes, Mia reads the person's request and the recorded steps
   and writes the final script: what changes from run to run becomes a copied value, an input or a loop.
3. **It's saved** under ▶ at the top of the side panel, with its steps in plain words, a Play button, and
   text boxes for its inputs.

When the person wants to save what a bot already did ("save that as an automation"), reply with no tasks and
`"automation": {"name", "about", "schedule", "steps"}`, copying the steps from the bots' recorded steps in the
context. It goes through the same make-it-reusable step before it's saved.

## Planning the recording task

Write the recording task's goal so the bot does **one item** of the job, the way it'll be repeated:

- Going through a list (search results, connections, rows): start on the list page, open the **first** item,
  do the job for it, and stop. Don't do the second one: the loop does the rest.
- Stop **before** the final irreversible step (Send, Post, Submit, Apply) unless the person said to do it
  on this first run. The script adds that step by its button text, and the person approves it on each run.
- Use the person's real words for anything typed. If the message should start with the person's first name,
  the bot types it for this one person; the script turns it into `{{first_name}}`.
- Start where the person is when that's the list they mean ("this search", "my connections tab"): give the
  task that tab's id. Otherwise give a url that opens the list directly, with filters in the address when the
  site allows it (for LinkedIn 1st connections: `https://www.linkedin.com/search/results/people/?network=%5B%22F%22%5D`).

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
    {"do": "copy", "css": "h1", "text": "Ana Silva", "as": "first_name", "words": 1},
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
only then for its recorded **css**. It retries for a few seconds while the page loads.

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
  recording run goes there. Never put it in the script: the script goes through the list, not that person.
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
  recorded is one person's name, which isn't on the next profile.
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

The panel shows the last run's result, for example `Step 4 (Click “Write a message”) didn't work: couldn't
find it on the page`. When the person asks to fix it:

1. Do that part of the job again with a bot, on the page where it failed, so the element is recorded as it is
   now.
2. Save a corrected script with the same name: replace the failing step's `text` with the words the page
   shows now, drop a css that changed, or add a `wait` before it if the page was still loading.
3. Never answer that they should do it themselves.

## Checklist before saving

- [ ] First step opens a page (the list page, for a loop).
- [ ] Nothing person-specific is hardcoded: names are copied, messages are inputs.
- [ ] Every `{{name}}` is copied earlier, an input, or `link` in a loop.
- [ ] Every targeted step has the visible `text`; long or id-filled css is dropped.
- [ ] A list job has `each` with a precise `links` and, if the list has pages, `next`.
- [ ] The final Send/Post step is there if the person wants it sent; they approve it on each run.
- [ ] `about` says in one sentence what it does, for whom, and whether it sends anything.
