# <Product>: agent guide

Read PROJECT_BRIEF.md before any non-trivial work. It is the source of truth.

## Scope
- Build the FREE tier only (brief §7). Paid features (§8) are parked.
- Ask Saad before resolving any open decision (§16).

## Non-negotiables
- Privacy invariants P1-P7 (§5): the SDK never captures prompts, outputs, keys,
  URL paths/queries or error message text, never makes extra calls, never
  modifies requests/responses, and always fails open.
- Security requirements (§11). Never log keys, Authorization headers or request bodies.
- Efficiency first, no bloat (§21). No new components, dependencies or abstractions
  without a measured need. The wire format is defined in §19.

## Working style
- Small PRs with tests. Run `make lint test` before finishing.
- Record non-trivial choices as ADRs in docs/decisions/.
- Verify SDK internals and external formats against current docs (§17).
- Keep responses concise. Saad will ask for detail.

## Commands
make dev · make test · make lint
