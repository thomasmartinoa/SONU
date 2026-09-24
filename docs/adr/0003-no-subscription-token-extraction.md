# ADR 0003 — Cloud models only through sanctioned access; no subscription-token extraction

- **Status:** accepted
- **Date:** 2026-09-24

## Context

The author wants SONU to be able to use strong cloud models (Claude, GPT, Gemini, Grok, …), ideally by "logging in
with an account" rather than paying for API keys. The author has a Claude Pro plan and a ChatGPT Go plan, and no paid
API credit.

Research on 2026-09-24 (sources in `docs/PROPOSAL.md` §2.7 and §13):

- **Anthropic**: using Claude Free/Pro/Max OAuth tokens "in any other product, tool, or service — including the Agent
  SDK — is not permitted"; enforced since April 2026.
- **Google**: using Gemini CLI OAuth from third-party tools "is a violation of applicable terms"; accounts have been
  suspended.
- **OpenAI**: ChatGPT sign-in is tolerated in third-party *coding* harnesses but is not contractual, only works
  against the Codex backend, and the **Go plan has no Codex CLI or API access at all**.

## Decision

1. SONU never extracts, proxies or reuses subscription OAuth tokens.
2. Cloud models are reached only through sanctioned access: API keys (including free tiers), Ollama Cloud's own
   sign-in, and local OpenAI-compatible servers.
3. To use a subscription, SONU becomes an **MCP server** that the vendor's own official client (e.g. Claude Code on a
   Claude Pro plan) can drive. That is ordinary, documented use of the vendor's product.
4. Every cloud call goes through the egress guard and respects `privacy.cloud` (default `never`).

## Consequences

- Users are not exposed to account bans because of SONU.
- SONU's own voice loop runs on local models or API-key models; subscriptions help via MCP-driven workflows.
- Provider terms are linked from the docs and re-checked when adapters are added, since they changed three times in
  2026 alone.
