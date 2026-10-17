# 0015: Ingest key format and the cached lookup

- **Status:** accepted
- **Date:** 2026-10-17

## Context
Brief §11.4 sets the outline: `gl_live_`, 32 random bytes in base62, a checksum "e.g. CRC32 in
base62", only the SHA-256 stored with `display_prefix` and `last4`, and a lookup cache of at
most 60 s so revocation takes effect within a minute. §9.2 adds a debounced `last_used_at`.
It leaves open the exact encoding, what the checksum covers, what the display fields hold and
how the cache behaves. The SDK stops sending after a 401 (`_sender.py`).

## Options considered
**Caching keys not found**
1. Cache them too: saves a lookup when a revoked key keeps sending, but made-up keys (anyone
   can compute a valid checksum) fill the cache and push real keys out.
2. Cache only keys found: a revoked key costs one lookup per SDK process, since the SDK stops
   after the 401.

**`last_used_at`**
1. A separate debounced `UPDATE` after each accepted batch, with its own timer per key.
2. Make the lookup itself `UPDATE ... SET last_used_at = now() ... RETURNING`, run only on a
   cache miss.

## Decision
- **Format:** `gl_live_` + 43 base62 characters (the 256-bit number, zero-padded; 62^43 >
  2^256) + 6 base62 characters of CRC32 over everything before it (62^6 > 2^32). 57
  characters. Alphabet `0-9A-Za-z`. The checksum covers the prefix, so a `gl_test_` key can
  never pass as a live one by changing the prefix.
- **Checks before any database work:** exact length, prefix, alphabet and checksum. CRC32
  catches every single-character change (tested exhaustively for one key).
- **Stored:** SHA-256 of the whole key, `display_prefix` = the first 12 characters
  (`gl_live_` + 4), `last4` = the last 4 (part of the checksum). Together they reveal under 48
  of the 256 bits.
- **Cache:** option 2. Found keys only, 60 s from before the query, at most 10,000 entries,
  oldest dropped first. Database errors propagate and are never cached, so an outage gives a
  `503` (step 3), not a `401` that would stop the SDK.
- **`last_used_at`:** option 2. One round trip per key per minute per machine, using only the
  `ingest_writer` grants from ADR 0013. Accurate to about a minute.
- The SDK keeps its looser check (`gl_` + letters, digits, `_`), so key formats can change
  without an SDK release.

## Trade-offs
- The lookup writes, so it needs a primary database, not a read replica. Fine on one Neon branch.
- `last_used_at` is set even if the request is then rejected (size, rate limit, schema): it
  means "last seen", not "last accepted".
- `ingest_writer` cannot read `projects`, so soft-deleting a project must revoke its keys in
  the same transaction (M4).
- Requests with made-up keys reach the database. The per-IP limit on invalid keys (step 4) and
  Cloudflare's rate limit bound them.
