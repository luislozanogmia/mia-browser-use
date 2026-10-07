# Chrome Web Store listing for Mia

Everything the Developer Dashboard asks for, ready to paste. Keep it truthful: the
store checks that the listing, the permissions and what the extension does agree.

## Store listing

**Name:** Mia

**Summary (132 chars max):**
Your browser assistant. Mia and her bots read, compare and act on your tabs with you, running on your own Mac with your Claude.

**Category:** Productivity → Tools

**Language:** English (Mia answers in the language you pick in Settings)

**Description:**

Mia is a browser assistant that works where you already are: in your tabs.

Open her side panel and ask about anything on a page. Drag over part of a page
(a chart, a table, a paragraph) and a bot explains it right there, on the page.
Select text and ask. Compare two tabs. Ask her to fill a form, collect rows into
a Google Sheet, or go through a list of pages one by one. Mia plans the job, her
bots do it in your tabs, and she asks you before anything that sends, posts,
buys, deletes or opens a site with data from a page.

Mia runs on your computer, not in the cloud. The extension talks to a small
helper installed on your Mac, and the helper answers with your own Claude
account through Claude Code (Anthropic). Your pages, your questions and the
answers never go to a Mia server, because there isn't one.

Setup is two steps: add Mia to Chrome, then run the Mia installer once
(the panel links to it). After that Mia starts by herself whenever Chrome
needs her.

Requirements: a Mac with Apple silicon (macOS 12 or newer), Google Chrome, and
a Claude account (Anthropic). Windows and Linux are not supported yet.

## Privacy practices

**Single purpose:** Mia lets a person ask about and act on the web pages open in
their own browser, with an assistant that runs on their own computer.

**Permission justifications**

- `tabs`: Mia lists your open tabs in her panel so you can pick which one a
  question or a job is about, and opens the tab a job needs.
- `activeTab` and `scripting`: when you ask about a page, Mia draws the question
  box and the answer card on that page, reads the text and the area you dragged
  over, and performs the clicks and typing you asked for. Nothing is injected
  into a page you didn't ask about.
- `<all_urls>` (host permission): you can ask about any site, so Mia can't know
  the list in advance. Scripts run only in tabs you ask about.
- `nativeMessaging`: the extension talks to the Mia helper installed on your
  Mac. That is how your Claude account is used without any Mia server.
- `storage` and `unlimitedStorage`: your settings, the chat history, and the Reel
  (screenshots you chose to keep) are stored in your browser. Screenshots are
  large, hence unlimitedStorage.
- `sidePanel`: Mia's chat lives in Chrome's side panel.
- Content script on `docs.google.com`: Google Docs draws text on a canvas, so a
  small script makes the document's text readable to Mia when you ask about it.
  It changes nothing in the document.

**Remote code:** No. All code ships in the package. The extension talks to the
helper on 127.0.0.1 only.

**Data use disclosure (tick these):**
- Website content: collected, used only for the feature the person asked for.
  Handled on the person's computer and sent to Anthropic through the person's
  own Claude account, under Anthropic's terms. Not sold, not used for anything
  but the question asked, not used for creditworthiness or lending.
- Personally identifiable information: not collected by Mia. Page content may
  contain it; it is handled as above.
- Everything else (health, financial, authentication, personal communications,
  location, web history, user activity): not collected.

Certify: Mia does not sell data to third parties, does not use data for
purposes unrelated to the single purpose, and does not use data to determine
creditworthiness or for lending.

**Privacy policy URL:** https://luislozanogmia.github.io/mia-browser-use/privacy.html
(file: store/privacy.html; publish it with GitHub Pages from this repo, or at
https://www.mia-labs.com/mia/privacy and change this line).

## Assets

- Icon 128×128: extension/icons/mia-128.png
- Screenshots 1280×800 (1 to 5): frames from mia-film/ (the panel, a question
  on a page, the answer card, an approval, the Reel). Re-render with the
  current panel first (no Share or Follow controls).
- Small promo tile 440×280: optional; the brand folder has the mark.

## Distribution

- Visibility: Public (or Unlisted while testing with a few people; the link
  still works and the Mac package still installs it).
- Regions: all.
- Pricing: free.
