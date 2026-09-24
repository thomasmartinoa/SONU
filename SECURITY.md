# Security policy

SONU is an agent that acts on your desktop, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting:
**Security → Report a vulnerability** on the repository page.

Include what you found, how to reproduce it, and what an attacker could achieve. You'll get an acknowledgement as
soon as possible, and credit in the release notes if you want it.

## In scope

- Ways to make SONU perform an action the policy should have blocked or asked about
- Prompt injection that changes what SONU does (e.g. through window titles, files, web pages)
- Leaks of personal data (window titles, journal, memory, audio, screenshots) outside what the user allowed
- Secrets appearing in logs, audit records or prompts
- Anything that can stall or crash the compositor through SONU
- Supply-chain issues in SONU's dependencies or CI

## Out of scope

- Harm caused by actions the user explicitly approved after they were shown accurately
- Attacks that require already controlling the user's account

## Design references

- [Threat model](docs/threat-model.md)
- [Safety defaults (ADR 0004)](docs/adr/0004-safety-defaults.md)
