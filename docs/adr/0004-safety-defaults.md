# ADR 0004 — Safety defaults: `confirm-risky`, local-only, fail closed

- **Status:** accepted
- **Date:** 2026-09-24

## Context

SONU acts on a real desktop. Mistakes by a small local model, prompt injection through content SONU reads (window
titles, web pages, files), and spoofed voice commands are all realistic. The author asked for "whatever is best".

## Decision

| Setting | Default | Why |
|---|---|---|
| `policy.autonomy` | `confirm-risky` | Low-risk actions (open/move/focus) flow without friction; closing, killing, deleting, writing, network egress and GUI input need confirmation. |
| `privacy.cloud` | `never` | Nothing leaves the machine until the user opts in. |
| Forbidden actions | always denied | Privilege escalation, reading `~/.ssh` / `~/.gnupg` / keyrings, editing SONU's own policy, loading compositor plugins — no mode or model output can enable them. |
| Invalid config | refuse to act | Falling back to defaults could silently *loosen* a setting the user tightened. Only `sonu doctor` and `sonu config` run with a broken config (implemented in M0). |
| Damaged audit log | refuse to append | An agent whose history can't be recorded shouldn't act (implemented in M0). |
| Voice confirmations | never for high-risk steps | A video or a nearby speaker can say "Hey Sonu, delete …". |

## Consequences

- Slightly more prompts than a "YOLO" agent; in exchange, the user can trust that SONU won't do something
  irreversible on its own.
- `trusted` mode exists for experienced users but still never skips high-risk confirmations or forbidden-action
  denials.
