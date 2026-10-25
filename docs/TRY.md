# Things to try on the laptop

Everything below was built and tested while the owner was away (PRs #52–#65). Each feature passed its unit tests and
the eval suite, and was tried on the laptop with throwaway browsers, scratch folders or silent test songs. **None of it
has been tried by the owner yet.** Tick a line once it worked; anything that doesn't becomes a "found on real
hardware" fix (see [`STATUS.md`](STATUS.md)).

Start with `sonu doctor`: the **Integrations** lines show what's set up (browser extension, local music) and what's
missing.

## Set up first (one time)

- [ ] **Browser extension** (tabs, pages, clicking, typing): `sonu browser install`, then
  - Chromium: `chrome://extensions` → Developer mode → Load unpacked → the folder it printed;
  - Zen: `about:config` → `xpinstall.signatures.required` = false; `about:addons` → ⚙ → Install Add-on From File →
    the `.xpi` it printed; allow "Access your data for all websites";
  - then `sonu browser status` → each browser "connected · N tabs · extension 0.3.0".
- [ ] **Notifications** ("what was that notification?"): add to `~/.config/sonu/config.toml`, then
  `systemctl --user restart sonud`:

  ```toml
  [notifications]
  remember = true
  ```

- [ ] **Local music**: put some songs in `~/Music`. Optional: `sudo pacman -S mpv-mpris` so the rice's media keys
  control SONU's music too.

## Train "Hey Sonu" on your voice (#94)

1. If "Hey Sonu" is on, say "stop listening for Hey Sonu" first.
2. `sonu voice train-wake` — say each line it shows, the way you'd call SONU (about 3 minutes, in a normal room).
3. It prints how many of your "Hey Sonu"s were heard before and after, and that no look-alike wakes it.
4. `systemctl --user restart sonud`, then "listen for Hey Sonu" and try it from across the room.

## The HUD (#71)

- [ ] press the SONU key and ask something: the HUD at the top centre shows listening → what you said → each step
  → the result, then hides
- [ ] "delete <a file you don't need>" from the key: the question appears with **Yes / No** buttons on the HUD
- [ ] switch the rice's theme: the HUD follows its colours

## Try by voice (or typed)

### Phase 2: hearing you right (#115–#122)
- [ ] "open Claude Code in dev works" · "open btop" · "open nvim in five" — the HUD shows the right names and numbers
- [ ] "is yay installed?" · "do I have obs?"
- [ ] By voice: "install cozy" (or any request with several matches) — SONU says the choices; answer "the second
  one", "number one", "the last one" or "none"; the HUD's buttons work too

### Projects and terminals (#52, #53)
- [ ] "open my SONU project" · "open planner.py in SONU at line 40"
- [ ] "run the tests in SONU" · "did the tests pass?"
- [ ] "what did I change in SONU?" · "my last commits"
- [ ] "open a terminal in dev_works"

### Layouts (#54)
- [ ] "save this layout as work" · "what layouts do I have?" · "restore work" · "forget the work layout"

### Messages and notifications (#55, #56)
- [ ] with WhatsApp or Telegram open: "do I have new messages?" · "any telegram messages?" · "how many unread emails?"
- [ ] (notifications on) "what was that notification?" · "read my notifications" · "forget my notifications"

### Browser — needs the extension (#58–#60)
- [ ] "what tabs do I have open?" · "switch to the YouTube tab" · "close the YouTube tabs" (asks first)
- [ ] "summarise this page" · "what does the github tab say about installing?" · select text, then "explain the
  selected text"
- [ ] "scroll down" · "scroll to the top" · "click <a link on the page>" · "type red shoes in the search box and press
  enter"

### History and bookmarks — no extension needed (#61)
- [ ] "what pages did I visit today?" · "find the page about <something> I saw yesterday"
- [ ] "what bookmarks do I have?" · "open my <name> bookmark" · "open that <something> page from my history" (asks)

### Files (#62–#64)
- [ ] "open my <file name>" · "where is my <file> pdf?" · "what files did I open yesterday?"
- [ ] on a file you don't need: "move <it> to documents" · "rename <it> to <new name>" · "delete <it>" (it goes to
  the trash) · "restore <it>"
- [ ] "copy the path of <a file>" · "copy my last screenshot" · "copy hello from sonu", then paste somewhere
- [ ] by voice: "where are my <files>?" — SONU says the first three with their folders ("… in Documents"), then
  "and N more" (#68)

### Inside the laptop (#72)
- [ ] "what's using my CPU?" · "why is my fan so loud?" · "what's eating my memory?" · "did anything crash today?"
- [ ] start something you don't need, then "kill <it>" (asks first; never the desktop's own programs)

### Your documents (#73)
- [ ] "summarise my <file> pdf" · "what does <notes file> say about <something>?" · a Word document too

### Proactive (#74)
- [ ] "run the tests in SONU" and let it run past 30 s: a notification says when it passed or failed
- [ ] "brief me" / "catch me up"; optionally `[briefing] daily = true` in the config for one every morning

### Modes and keys (#92)
- [ ] `sonu control` → Brain → Mode: Balanced asks first; Fully local doesn't; "your own mix" when you've tuned it
- [ ] Brain → API keys: paste a key, press Enter → "✔ key" (kept in ~/.local/share/sonu/keys, since no keyring runs)

### SONU's voice from the cloud (#91)
- [ ] with a Sarvam key: Control Center → Voice → Who speaks = sarvam, Cloud voice = ritu (or shubh, kabir…), Brain → Use the cloud for → speaking (asks); then "what's my battery?"
- [ ] "any new messages?" is still said in Kokoro's voice (private)

### Hearing in the cloud (#90)
- [ ] a Sarvam key: `sonu settings key sarvam`; then `sonu voice check -m large-v3-turbo -m sarvam` — read ten lines, see which hears you better
- [ ] if Sarvam wins: Control Center → Voice → Who hears you = sarvam, and Brain → Use the cloud for → hearing (asks)

### Cloud brains (#87)
- [ ] `sonu settings providers` — which have a key; `sonu settings key openrouter` (a free OpenRouter account works)
- [ ] Control Center → Brain: Cloud provider = openrouter, Cloud model = a `:free` model, Cloud models = allow (asks), Use the cloud for = planning — then a long request plans in the cloud (`sonu audit tail` shows cloud.send)
- [ ] read [MODELS.md](MODELS.md) for which model fits which job

### The new HUD (#86, #89)
- [ ] press the SONU key: the island's bars bounce while it listens, the glow sweeps its edge while it works, dots fill with the steps
- [ ] a long answer: the HUD stays until SONU stops talking, then hides a few seconds later

### A second try (#85)
- [ ] with Zen closed: "move zen to workspace 4" — "↻ Tried another way", Zen opens on 4
- [ ] "close the thunar window" with Thunar closed — nothing gets opened

### The brain switch (#84)
- [ ] Control Center → Brain → "For harder requests": same (recommended on this laptop) or deep
- [ ] a new model you pull: `sonu eval --harder <model> -c complex-three-questions` before switching

### Your data in the Control Center (#83)
- [ ] `sonu control` → Memory, Conversations, Activity, Audit log: what SONU keeps, and a delete that asks first
- [ ] in a terminal: `sonu memory list`, `sonu conversations list yesterday`

### The Control Center (#82)
- [ ] `sonu control` (or say "open your settings"): Brain, Voice, Proactive, Privacy, Look
- [ ] switch heads-ups on; Cloud models → allow asks "are you sure?" first — then set it back to never
- [ ] a keybind for it, if you like: `bind = SUPER, comma, exec, ~/.local/bin/sonu control` (your dotfiles — your step)

### Settings without the file (#81)
- [ ] `sonu settings list` — everything you can change, with its value
- [ ] `sonu settings set heads_up.enabled on` — sonud restarts by itself; `sonu settings set privacy.cloud allow` asks first

### Corrections mid-sentence (#80)
- [ ] by voice: "open kitty on workspace 3, no, 4" · "play lofi on spotify, actually on youtube"
- [ ] "open kitty, sorry to bother you" still just opens kitty

### Heads-ups (#79)
- [ ] in `~/.config/sonu/config.toml`: `[heads_up]` `enabled = true` (and `speak = true` to hear them), then `systemctl --user restart sonud`
- [ ] unplug and let the battery fall to 15%: "Battery's at 15% — time to plug in." once
- [ ] the same during a Discord call: shown only, not said

### Sounding like a person (#78)
- [ ] "kitty 2" — a short answer; "ugh, open kitty already" — a plain one
- [ ] "good morning" in the evening; "how's it going?" — short, no "anything else I can do?"
- [ ] your name (if SONU knows it): now and then in a chat, not in front of every reply

### Conversations remembered (#77)
- [ ] ask a few things, then the next day: "what did we talk about yesterday?"
- [ ] after "find out how kestrels hunt…": "what did you tell me about kestrels?"
- [ ] "forget our conversations" asks first, then "what did we talk about today?" finds nothing

### Talking sooner (#76)
- [ ] press the SONU key, "find out how kestrels hunt and summarise it": "One moment." while it reads, and the answer starts after about a second
- [ ] a long reply: it should sound like one answer, with no odd gaps between sentences

### Research (#75)
- [ ] "find out <something> and summarise it" — the answer names the sites it read
- [ ] "look up <something> on github" still opens GitHub's results in the browser

### Music (#65)
- [ ] "play my music" · "shuffle my music" · "play <artist> from my music" · "pause" · "next song" · "what's
  playing?" · "stop the music"

## Before #52 and still 🧪

[`CAPABILITIES.md`](CAPABILITIES.md) marks everything built but not yet confirmed on the laptop with 🧪 — among them
reminders, screenshots, laptop health, Claude Code by voice, GitHub merges, pressing buttons through accessibility
and the cloud models. When one is confirmed, change its 🧪 to ✅ there.
