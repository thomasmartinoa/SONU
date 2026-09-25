# Using SONU with the hypr-dotfiles rice (Hyprland + Quickshell)

This guide wires SONU into the [hypr-dotfiles](https://github.com/thomasmartinoa/hypr-dotfiles) rice: a key on the
laptop that opens a SONU prompt, an "Ask SONU" row in the `SUPER+SPACE` menu, and notifications through the shell.
SONU only *reads* the rice's files; everything below is a change you make yourself.

## 1. Install `sonu` on your PATH

The rice puts `~/.local/bin` on Hyprland's `PATH`, so installing SONU as a uv tool makes it available to keybinds
and the menu. `--editable` means `git pull` in the repo updates the installed command.

```sh
cd ~/dev_works/SONU
uv tool install --editable .
sonu --version
sonu doctor
```

## 2. Local models

```sh
ollama pull qwen3.5:4b     # default planner (fast, fits in 8 GB VRAM)
ollama pull qwen3.5:9b     # deep planner: `sonu --deep …`
```

Then measure instead of guessing — this runs 20 realistic requests against a fake desktop (your real windows are
never touched):

```sh
sonu eval                                   # your configured planner
sonu eval -m qwen3.5:4b -m qwen3:8b         # compare models
```

## 3. The SONU key (NitroSense key)

The NitroSense key sends scancode `0xf5`. Depending on the Nitro model, Linux reports it as `XF86Launch1`
(models with a specific systemd hwdb entry), `XF86Presentation` (generic Acer rule), or not at all (it needs a vendor
driver such as [Linuwu-Sense](https://github.com/0x7375646F/Linuwu-Sense)).

**Measured on the author's Nitro:** `wev` shows `key: 156 … sym: XF86Launch1` with separate *pressed* and
*released* events — so the key works out of the box, and it can later drive push-to-talk.

To check yours:

```sh
sudo pacman -S --needed wev
wev          # press the NitroSense key, look for a line like "sym: XF86Launch1"
```

Then add one line to `~/hypr-dotfiles/hyprland/.config/hypr/modules/binds.lua` (use the `sym` you saw):

```lua
hl.bind("XF86Launch1", hl.dsp.exec_cmd("sonu prompt"))   -- SONU prompt (NitroSense key)
-- no usable key? SUPER+A is free in this rice:
-- hl.bind(mainMod .. " + A", hl.dsp.exec_cmd("sonu prompt"))
```

`sonu prompt` opens a one-line rofi box (rofi is part of the rice's fallback stack), runs the request, asks before
anything risky with a **No-first** yes/no box, shows a pick list when a name matches several apps or windows, and
shows the verified result as a notification.

In milestone 3 the same key becomes push-to-talk:

```lua
-- hl.bind("XF86Launch1", hl.dsp.exec_cmd("sonu listen start"))
-- hl.bind("XF86Launch1", hl.dsp.exec_cmd("sonu listen stop"), { release = true })
```

## 4. "Ask SONU" in the `SUPER+SPACE` menu

The rice's menu supports input rows: the search box becomes a text field, and Enter runs the action with `{}`
replaced by your text, safely single-quoted. Add this to `~/.config/hypr-theme/menu.local.jsonc` (create the file
if it doesn't exist; it is merged by id and hot-reloaded):

```jsonc
{
  "sonu": {
    "icon": "󰚩",
    "label": "Ask SONU",
    "description": "assistant ai sonu open move close windows workspace",
    "input": true,
    "action": "sonu ask --notify --confirm gui {}"
  }
}
```

## 5. Settle names that match several apps

With both Code - OSS and VSCodium installed, "open VS Code" is ambiguous, so SONU asks which one you mean instead of
guessing: a numbered list in the terminal, or a pick list from the key/menu. To decide once, add an alias to
`~/.config/sonu/config.toml` (`sonu config init` creates the file):

```toml
[apps]
aliases = { "vs code" = "code-oss.desktop", "vscode" = "code-oss.desktop" }
```

Desktop-file IDs are the file names in `/usr/share/applications` (or `~/.local/share/applications`).
Remembering this automatically when you answer ("use Code - OSS from now on") is part of milestone 2 (memory).

## 6. What SONU uses from the rice

| Rice file | SONU uses it for |
|---|---|
| `~/.config/hypr-theme/defaults/browser` (fallback: `zen-browser`) | "open my browser" |
| `~/.config/hypr-theme/defaults/terminal` (fallback: `kitty`) | "open a terminal", terminal apps like btop |
| `~/.config/quickshell/shell.qml` present | detection only (bar widget planned for M3) |

## 7. Try it

```sh
sonu --dry-run open kitty on workspace 2          # shows the plan, changes nothing
sonu open kitty on workspace 2
sonu open VSCodium and Chromium, put VSCodium on workspace 3 and Chromium on workspace 2
sonu "what's open right now?"
sonu close kitty                                  # asks first: closing is medium risk
sonu audit tail                                   # everything SONU did, hash-chained
```

If something goes wrong, `sonu -v …` shows more detail and `sonu doctor` checks the setup.
