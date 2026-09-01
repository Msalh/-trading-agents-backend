# PROMPT_VERSION Content-Hash Design — v1 (design and test plan only)

**Status: NOT ACTIVATED / NOT DEPLOYED.** This document specifies how `app.news_agent.
PROMPT_VERSION` and `app.macro_agent.PROMPT_VERSION` would move from a hand-maintained
marker (`"1"`, bumped manually whenever `SYSTEM_PROMPT` changes) to a content hash computed
from the prompt text itself. It changes no code. Both constants remain exactly `"1"`, set by
hand, today. Writing this document does not authorize implementing or deploying it — per the
nineteenth external review's explicit instruction, this stays documentation-only while the
registered prospective experiment (`51c4fadb-5a90-408e-a106-b41117417c1d`) is active.

## Why

The seventeenth review flagged the real gap: a prompt edit that changes what "urgent" or
"risk_off" or a direction/confidence actually means can go live without anyone remembering to
bump `PROMPT_VERSION` by hand — nothing enforces the marker actually reflects the prompt text.
The eighteenth review's initial framing deferred fixing this until the registered experiment
resolves, *unless* a separate proof showed the change couldn't affect any locked comparison,
drift classification, candidate inclusion, or stopping-rule progress — explicitly allowing the
*design* to be done now so it's ready, without deploying it. The nineteenth review corrected
that second branch after reviewing this document's own comparison logic (see section 7): it
isn't a live option, only resolution is. This document reflects that correction throughout.

## 1. What gets hashed

The **runtime `SYSTEM_PROMPT` string** as it exists in the running Python process — i.e.
`app.news_agent.SYSTEM_PROMPT` / `app.macro_agent.SYSTEM_PROMPT` after the module has been
imported and the string literal constructed — encoded as **UTF-8**, not the raw bytes of
`app/news_agent.py`/`app/macro_agent.py` on disk. Hashing the source file would make the
identity sensitive to things that have nothing to do with what the model actually receives:
a checkout with different line endings (CRLF vs LF), a change to a comment or to code
elsewhere in the file, or how the file happens to be encoded on disk. Hashing the constructed
runtime string is what a model call actually sends, and is what "did the prompt's meaning
change" should track.

## 2. Hash algorithm and format

Full **SHA-256** digest (`hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()`, 64 hex
characters), stored with an explicit algorithm prefix: `sha256:<64 hex chars>`. The prefix
makes the value self-describing — a future migration to a different algorithm produces a
visibly different-shaped string rather than a same-length hex string that silently means
something else. No truncation: a shortened hash trades a meaningful amount of collision
resistance for a few saved characters in a field that's compared for exact equality, never
displayed to a human as an identifier they need to type.

**No normalization after the Python string literal is constructed** — no stripping,
whitespace collapsing, Unicode normalization (NFC/NFKC), or line-ending rewriting applied to
`SYSTEM_PROMPT` before hashing. Any runtime change to the prompt text, including one that
looks cosmetic (an extra blank line, a re-wrapped sentence), changes the hash. This is
deliberate: the whole point is that a human no longer has to judge which edits are "real"
enough to bump a version by hand — the hash makes that judgment call disappear.

## 3. Scope: prompt identity only, not a general config hash

This hash identifies the `SYSTEM_PROMPT` text only. It does **not** fold in `MODEL`, any
sampling parameter, the web_search tool schema/config Claude is given, or the required JSON
response shape drift detection already partly infers from `PROMPT_VERSION` today. Those stay
separate, their own provenance fields, tracked (or left for future tracking) independently —
silently blending them into one "prompt" hash would make a drift report say "prompt changed"
when actually the model or tool config changed, or vice versa, which is a worse diagnostic
than today's single hand-maintained marker, not a better one. `app.macro_agent_v2`'s own
`MACRO_V2_SCHEMA_VERSION="2"` marker is a separate, already-independent versioning axis (Tier
3.44) and is out of scope here — this design only concerns
`app.news_agent.PROMPT_VERSION`/`app.macro_agent.PROMPT_VERSION`.

## 4. Historical data: no retrospective rewrite

Every opinion already stored (in `agent_opinions`, and therefore every candidate's frozen
`decision.opinions_used` snapshot) keeps its `prompt_version` value exactly as recorded —
`"1"` — permanently. This design never rewrites history to backfill a hash for opinions
generated before the cutover. `_classify_opinion_drift()` in `app.prospective_experiments`
(line 338) compares `opinion["prompt_version"] == locked_prompt_version` by plain string
equality with no assumption about the value's shape — a hash string works as a drop-in
replacement with zero changes to that comparison logic. The only thing that changes is what
gets written into new opinions going forward.

## 5. Cutover semantics

At the moment of cutover, `PROMPT_VERSION` (currently the literal `"1"`) is reassigned to the
computed hash string. A rolling deployment where an old worker process (still holding
`PROMPT_VERSION = "1"` in memory) and a new one (already holding the hash) briefly generate
opinions concurrently would make "the first opinion generated after the code deploys"
ambiguous — deploy time and generation time aren't the same instant across processes. This
design picks the strict-boundary option rather than the mixed-window one: **old workers are
drained (stopped accepting new opinion-generation traffic) before any new worker starts
accepting it**, so there is a single, unambiguous cutover instant, not a window during which
either identity could apply to a given opinion depending on which process happened to handle
it. Every opinion generated before that instant keeps `"1"` (or whatever hand-maintained
marker it already has) forever; every opinion generated at or after it carries the new hash.
There is no dual-write, no backfill, and no attempt to retroactively decide which pre-cutover
opinions "really" match the new hash's prompt text. A prospective experiment registered before
cutover and still active after it would see this as ordinary `prompt_version` drift (an
opinion whose `prompt_version` no longer matches what was locked at registration) — exactly
the mechanism that already exists today, unaffected by this change.

## 6. Test plan

All of the following are specification only in this document — no test files are added to
`tests/` as part of this tier; this is the plan a future implementing tier would execute
against, per the nineteenth review's "documentation-only, no dormant runtime behavior"
instruction.

The hashing itself is a small, pure function of its input text —
`app.prompt_hash.hash_prompt(text: str) -> str` (see the Summary section below). Its
permanent unit-test vectors should be **fixed fixture inputs**, not the live production
prompts: pinning tests to the live `SYSTEM_PROMPT` text would mean every legitimate prompt
edit also has to update a snapshot assertion, reintroducing exactly the manual-maintenance
burden this whole design exists to remove — just moved into CI instead of removed. Durable
`hash_prompt()` vectors, computed the same way (`hashlib.sha256(text.encode("utf-8")
).hexdigest()`, `sha256:` prefix), on fixed inputs chosen to exercise the edge cases that
matter for a hash function (empty input, ASCII, non-ASCII/multi-byte UTF-8, embedded
newlines):

| Fixture input | SHA-256 |
|---|---|
| `""` (empty string) | `sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `"abc"` | `sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad` |
| `"héllo wörld 日本語"` (non-ASCII, multi-byte UTF-8) | `sha256:a0a3f8b70ea8c54c8659eb695ca59ebe216bca22f585c64307566406e4f6e483` |
| `"line one\nline two\nline three"` (embedded newlines) | `sha256:26a5cd654e540e91433a2f237e2709743fc4753e764deb74ed37299c2f338ece` |

(The empty-string vector is also the well-known standard SHA-256-of-empty-string value,
useful as a cross-check that the encoding/prefix wrapper hasn't altered the underlying
digest.) A future `hash_prompt()` implementation's unit tests assert against this table
directly and never need to change when `SYSTEM_PROMPT` is edited.

**Provenance/reference hashes (not durable test assertions).** Computed against the exact
`SYSTEM_PROMPT` strings live in this repo as of Tier 3.48 (`origin/main` `03a2204`):

| Agent | `SYSTEM_PROMPT` length (UTF-8 bytes) | SHA-256 as of `03a2204` |
|---|---|---|
| `app.news_agent` | 1347 | `sha256:d213ffbe2cc693b53806b2696cb78850cf7444c7a102f5251bbcd3b636708275` |
| `app.macro_agent` | 1413 | `sha256:49bf639567ed1e549f3824aa75a89d05c6e189fd2fd8bef4161396de27c897ea` |

These record what the live prompts hash to as of this document, for provenance — they are
NOT deployed anywhere (`PROMPT_VERSION` is still `"1"` in the running code today) and must
NOT become a permanent CI assertion that the production prompt equals this value forever.
The one place they're worth asserting at all is a **cutover-specific test**, written and
updated at the moment of actual implementation, whose entire purpose is confirming "the hash
computed at deploy time matches what we expect the current prompt to produce" — a test that
is *deliberately* rewritten whenever the production prompt legitimately changes, not a
tripwire meant to catch that change as a bug.

**Determinism test.** Hashing the same text twice (or across two separate Python
processes/imports) must produce an identical digest via `hash_prompt()`. `hashlib.sha256` is
already deterministic by construction, but the test exists to pin the *function this project
defines around it* (encoding choice, prefix format), not to re-test the standard library.

**One-character-change test.** `hash_prompt(text)` and `hash_prompt(text + ".")` must differ.
Using the live News prompt as the example text (not as a fixed fixture — see above), appending
a single trailing period changes the digest from
`sha256:d213ffbe2cc693b53806b2696cb78850cf7444c7a102f5251bbcd3b636708275` to
`sha256:73732a267c1ff230c5fc4760e169d35788537e93492d2198326afa6fc29b3634` — visibly unrelated
to the unmodified vector, as expected of SHA-256 (no meaningful bit-similarity between
near-identical inputs). Proves the hash is sensitive enough that no prompt edit, however
small, goes unnoticed.

**Unrelated-configuration-change test.** Since `hash_prompt()` is a pure function of its text
argument alone, this reduces to a simple, direct assertion rather than a module-monkeypatching
exercise: `hash_prompt(text)` called with `MODEL`/`DECISION_THRESHOLD`/`WEIGHTS`/
`SLIPPAGE_POINTS`/etc. varied around it (or not passed at all — the function doesn't take
them) must still return the same value for the same `text`. No monkeypatching of whole
modules needed; passing identical text is sufficient to demonstrate the function has no other
inputs. Separately, an **integration-level test per agent** (News, Macro) should confirm that
each module actually derives its stored marker from *its own* `SYSTEM_PROMPT` and nothing
else — that's a different claim (wiring, not the hash function) and belongs in each agent's
own test file, not `hash_prompt()`'s.

**Independent identities.** News and Macro use different `SYSTEM_PROMPT` text (confirmed
above — different byte lengths, different hashes) and must always be hashed and stored
independently; the per-agent integration tests above should assert the two computed markers
are never equal and that editing one module's prompt never changes the other's stored value.

## 7. Deployment gating

**Activation waits until the registered prospective experiment
(`51c4fadb-5a90-408e-a106-b41117417c1d`) has resolved. Full stop — no earlier path.**

An earlier draft of this section offered a second gate: a "separate proof" that changing the
stored `prompt_version` value can't affect locked comparisons, drift classification, candidate
inclusion, or stopping-rule progress for the currently-active experiment, citing section 4's
plain-equality comparison as evidence such a proof might exist. That was backwards. Section 4
itself already shows the opposite: the experiment locked `prompt_version = "1"` at
registration (see `_current_locked_config()`); if cutover happened while it's still active,
every opinion generated afterward would carry `sha256:...` instead of `"1"`, and
`_classify_opinion_drift()`'s plain-equality check (line 338) would classify every one of them
as drifted — not proof of safety, proof that an early cutover would actively corrupt this
experiment's population going forward. There is no version of "deploy now" that doesn't do
that while `51c4fadb-...` is open. There is no benefit to keeping a theoretical early-cutover
path alive when the document's own comparison logic already rules it out — the only real
gate is resolution.

## Summary — what activation would require

A future tier implementing this: reassign `app.news_agent.PROMPT_VERSION` and
`app.macro_agent.PROMPT_VERSION` from their current hand-maintained string literals to a
computed `sha256:<hex>` value (a small shared helper function,
`app.prompt_hash.hash_prompt(text: str) -> str`, rather than duplicating the hashlib call in
both modules); a drained rolling deployment so the cutover instant is unambiguous (section 5);
the fixture-based `hash_prompt()` unit tests plus the per-agent integration tests from
section 6, actually written this time; and confirmation that the registered prospective
experiment (`51c4fadb-5a90-408e-a106-b41117417c1d`) has resolved before merging, per section
7 — the only gate. Until that tier exists and is reviewed on its own, `PROMPT_VERSION`
remains exactly what it is today: a hand-maintained `"1"` for both agents.
