# Mia Browser Use — Product

People and AI bots browse together. A Chrome extension talks to a local bridge, the bridge joins a room, and everyone in the room sees the same shared pages, each other's cursors, and the bots' answers drawn on the page.

## What it does today

- **Ask about anything on a shared page.** Select text, or crop an area (Alt+Shift+A), and ask the bots. The answer appears as a card anchored on that spot, on everyone's screen in the room.
- **Conversations.** Each answer card has a Reply box. Follow-ups stay in the same thread and reach the bot with the original selection, the picture of the cropped area, the page, and the earlier turns.
- **Links.** An ask carries the links inside the selection or crop (up to 8), so a bot can open them when asked.
- **Immersive mode.** The pointer wears Mia's face (a hand on links and buttons, the text cursor in fields), so it's clear the mode is on. A click is a normal click on the page; only a drag becomes a crop, which shows a cross while you drag. Esc pauses, Alt+Shift+A resumes.
- **Skip the question.** Crop or select, and the bot explains it right away, with no question box.
- **Language.** A box in the side panel's settings, English by default. Bots reply in that language. Only a language name is accepted, so it can't carry instructions to the bot.
- **Cards stay in view.** Cards sit next to what they're about, slide past each other, and open upward when there's no room below (for example a call's control bar).
- **Reel.** Every shared page visited and every answer is saved as a screenshot, in order, in this browser only.
- **Reel to PDF.** The reel's **Make PDF** button turns it into a report. Claude Sonnet 5.5 reads a text-only digest (never the screenshots) and writes the title, summary, three takeaways, chapters, and a short caption per moment. The page lays it out about 70% pictures and 30% text, with no text under 12px. Where a diagram makes a point clearer than a screenshot, the model can ask for one (key numbers, steps, comparisons, bar charts) using only facts from the reel. The print dialog then saves the report as a PDF. Without the bridge or the model, the report is still made, with plain chapters by site.

- **Mia's chat (side panel).** Click the Mia icon in the toolbar, or press Alt+Shift+M. The gear in the panel holds the settings: connection, sharing, Follow me, Reel, Immersive, Skip the question and Language. It always knows the tab you're on and the text you selected (and the links inside it).
  - **Ask** reads the page and answers. It never touches the page.
  - **One agent per tab.** Each tab gets one agent (Mia 1, Mia 2…) with its own color and one mote on the page. Everything that happens on that tab is a task of that agent: a selection or crop you ask about, an **Ask** or a **Do** from the chat. An agent does its tasks one at a time; different tabs work side by side (up to 4 at once).
  - The **Agents** list in the panel shows every agent and its tasks, whatever started them: what it's doing now, what's waiting, what's done. Click an agent or a task to go to its tab.
  - **Do** turns your request into tasks. A task with a link opens its own background tab, with its own agent, so your view never moves. A task without one goes to your current tab's agent.
  - **Parallel** lets tabs work at the same time. **One by one** runs Do tasks in order.
  - **Approval:** anything that sends, posts, submits, buys, deletes, follows or connects stops as "needs you". You get Approve / Reject / Show me, and the mote waits on the exact button it wants to press. Workers never type passwords.
  - Workers are Claude processes with no tools of their own, started from an empty folder. Each turn a worker asks the bridge for one browser action, and the bridge runs it only on that worker's tab. There's no MCP, and `ghost_eval` is never available. The panel only talks to the bridge, so Mia can reuse it as is.
  - The model defaults to Sonnet 5.5. Opus 5.5 and Haiku 4.5 are in the picker.

- **Mac installer.** `packaging/build-pkg.sh` builds `Mia-Browser-Use.pkg`, a double-click installer that brings its own Python, so nothing else is needed first. It then opens a page in Chrome that shows how to add the extension. Once the extension is on the Chrome Web Store, building with `GHOST_WEB_STORE_ID` makes Chrome offer it by itself. Apple Silicon Macs only for now.
- **Claude account.** Mia answers with the person's own Claude account through Claude Code. The installer installs Claude Code if it's missing, and the first time Mia's panel shows **Sign in to Claude**, which opens the normal Claude sign-in in the browser. Nothing happens in Terminal. This is for the MVP: later, Mia runs on the same core as Mia Multiplayer (Hermes Agent).
- **Starts by itself.** After the installer, the extension starts Ghost whenever it can't find it, including when Chrome opens. One background program (`mia-browser-use up`) runs the bridge and the room, and restarts either if it stops. A fresh install gets a personal room named after the Mac user, so there's nothing to set up, paste or run. Logs go to `~/.ghost/bridge.log`. `ghost_eval` is never turned on this way.

## Principles

- Everything a web page or another person sends is untrusted data. It is never followed as instructions and never drawn as HTML, and it is validated on both the extension side and the room side.
- The bot's model runs with no tools, from an empty folder.
- Screenshots and cropped pictures stay on the machine where they were taken.
- Only two browser targets: the Chrome extension bridge and the Hermes Desktop browser. Listeners stay local, tokens stay out of URLs and logs, and every request size is bounded.

## Roadmap: listening to video calls (feasibility, not built)

**Goal:** the bot listens to a video call and turns it into status updates and items to work on, in real time.

**Verdict:** feasible, and most of the plumbing exists. OpenSuperWhisper itself is the wrong tool. It's a push-to-talk dictation app for macOS that only hears your microphone (not the other people on the call), records only while a key is held, and has no API. What is worth borrowing is its local engines: Whisper (via whisper.cpp) and Parakeet. Both run faster than real time on Apple silicon.

### Three ways to hear the call

| Path | Covers | Effort | Notes |
|---|---|---|---|
| **A. Read the live captions on the page** | Google Meet and Teams in the browser | ~1 day | Cheapest. The extension already reads pages, and captions come with speaker names. Breaks when Google or Microsoft change their page layout, and depends on captions being turned on. |
| **B. Capture the call's tab audio and transcribe locally** | Any call in Chrome (Meet, Zoom web, Teams web) | ~3–5 days | Chrome's `tabCapture` gets everyone else's audio and the mic gets yours, so "you vs. others" comes free. Audio goes to the bridge, a local Whisper or Parakeet process turns it into text, and audio never leaves the Mac. Chrome shows its "sharing this tab" indicator, and the user clicks to start. |
| **C. Desktop Zoom or Teams apps** | Native apps | +1 week | Needs a small Mac helper that captures system audio (Apple's ScreenCaptureKit, macOS 13+), plus Screen Recording permission. The existing native host is a starting point. |

A hosted meeting bot that joins as a participant (Recall.ai, for example) also exists. It sends the audio to a third party and costs money, so it's not recommended.

### How it would fit into Ghost

1. Transcribe in 5–10 second chunks, skipping silence. Text lags speech by about 2–5 seconds.
2. Every minute, or at each pause, Sonnet reads the running transcript and pulls out status updates, action items with owners, decisions, and open questions, in the user's Language setting.
3. They land in the room as live cards that everyone sees, and the transcript and items go into the reel, so the PDF report covers the meeting.
4. Only text goes to the model; audio stays on the Mac.

### What could stop it

- **Consent:** recording laws differ, and some places require everyone on the call to agree. Ghost should show a visible "transcribing" indicator and make it easy to tell participants.
- **Telling people apart:** separating individual voices from one mixed track is hard and slow. Path A gets names from the captions; path B only separates you from everyone else.
- **Chrome rules:** `tabCapture` needs an extension page running in the background and a click to start, which means two new extension permissions. Audio would travel over the existing extension–bridge connection with size limits, so no new transport is added.

### Recommendation

Start with **A** as a one-day prototype on Meet to see whether live action items are useful. If they are, build **B** as the real version: it works for any call in the browser and keeps audio local. Take on **C** only if the desktop apps are needed.

Sources: [OpenSuperWhisper on GitHub](https://github.com/starmel/OpenSuperWhisper), [OpenSuperWhisper releases](https://github.com/Starmel/OpenSuperWhisper/releases).
