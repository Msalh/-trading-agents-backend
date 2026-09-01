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
The eighteenth review deferred fixing this until the registered experiment resolves, unless a
separate proof shows the change can't affect any locked comparison, drift classification,
candidate inclusion, or stopping-rule progress — explicitly allowing the *design* to be done
now so it's ready, without deploying it. The nineteenth review confirmed that framing and
specified exactly what the design/test plan needs to cover; this document is that plan.

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
computed hash string. **The first opinion generated after that code deploys** carries the new
hash identity; every opinion generated before it keeps `"1"` (or whatever hand-maintained
marker it already has) forever. There is no dual-write, no backfill, and no attempt to
retroactively decide which pre-cutover opinions "really" match the new hash's prompt text —
the cutover is a hard boundary at deploy time, the same way any other frozen-at-generation
field in this project already works. A prospective experiment registered before cutover and
still active after it would see this as ordinary `prompt_version` drift (an opinion whose
`prompt_version` no longer matches what was locked at registration) — exactly the mechanism
that already exists today, unaffected by this change.

## 6. Test plan

All of the following are specification only in this document — no test files are added to
`tests/` as part of this tier; this is the plan a future implementing tier would execute
against, per the nineteenth review's "documentation-only, no dormant runtime behavior"
instruction.

**Fixed test vectors.** Computed against the exact `SYSTEM_PROMPT` strings live in this repo
as of Tier 3.48 (`origin/main` `03a2204`), using `hashlib.sha256(SYSTEM_PROMPT.encode("utf-8"
)).hexdigest()`:

| Agent | `SYSTEM_PROMPT` length (UTF-8 bytes) | SHA-256 |
|---|---|---|
| `app.news_agent` | 1347 | `sha256:d213ffbe2cc693b53806b2696cb78850cf7444c7a102f5251bbcd3b636708275`[^1] |
| `app.macro_agent` | 1413 | `sha256:49bf639567ed1e549f3824aa75a89d05c6e189fd2fd8bef4161396de27c897ea`[^1] |

[^1]: These are reference vectors for a *future* implementation's tests to assert against —
they are NOT deployed anywhere and `PROMPT_VERSION` is still `"1"` in the running code today.
If `SYSTEM_PROMPT`'s text changes before this is implemented, these vectors must be
regenerated (that's the entire point of the hash — it's supposed to change).

A future test suite should assert `hash_prompt(app.news_agent.SYSTEM_PROMPT) ==
"sha256:d213ff...08275"` and the equivalent for Macro, so an accidental, unnoticed edit to
either prompt string fails CI immediately rather than silently shipping under a stale marker
— catching exactly the failure mode the seventeenth review originally flagged.

**Determinism test.** Hashing the same `SYSTEM_PROMPT` value twice (or across two separate
Python processes/imports) must produce an identical digest. `hashlib.sha256` is already
deterministic by construction, but the test exists to pin the *function this project defines
around it* (encoding choice, prefix format), not to re-test the standard library.

**One-character-change test.** Hashing `SYSTEM_PROMPT` with a single trailing character
appended must produce a different digest, confirmed different from the base vector above:
appending `"."` to the News prompt used in testing produced `sha256:73732a267c1ff230c5fc4760e
169d35788537e93492d2198326afa6fc29b3634` — visibly unrelated to the unmodified vector, as
expected of SHA-256 (no meaningful bit-similarity between near-identical inputs). Proves the
hash is sensitive enough that no prompt edit, however small, goes unnoticed.

**Unrelated-configuration-change test.** Changing something outside `SYSTEM_PROMPT` — `MODEL`,
`DECISION_THRESHOLD`, `WEIGHTS`, `SLIPPAGE_POINTS`, an unrelated import — must leave the
computed prompt hash unchanged, since the hash function only ever reads the `SYSTEM_PROMPT`
string. This is what enforces section 3's scope boundary: the test should construct a
monkeypatched module state with prompt text held fixed and everything else varied, and assert
the hash is invariant.

**Independent identities.** News and Macro use different `SYSTEM_PROMPT` text (confirmed
above — different byte lengths, different hashes) and must always be hashed and stored
independently; a future test should assert the two hashes are never equal and that changing
one prompt never changes the other's stored value.

## 7. Deployment gating

Deployment (reassigning either `PROMPT_VERSION` constant from its hand-maintained string to a
computed hash) stays deferred until **one of**:

- The registered prospective experiment resolves, or
- A separate, explicit proof — its own reviewed diff and its own tests — demonstrates that
  changing the stored `prompt_version` value cannot affect any locked comparison, drift
  classification, candidate inclusion, or stopping-rule progress for the currently-active
  experiment. Section 4 above (plain-equality comparison, no retrospective rewrite) is a
  strong argument this is already safe, but per the nineteenth review, that argument itself
  needs to be the "separate proof," not asserted here as sufficient on its own.

Given the risk of touching a currently-active experiment's config-drift surface is avoidable
simply by waiting, waiting for resolution remains the preferred cutover path over rushing a
safety proof.

## Summary — what activation would require

A future tier implementing this: reassign `app.news_agent.PROMPT_VERSION` and
`app.macro_agent.PROMPT_VERSION` from their current hand-maintained string literals to a
computed `sha256:<hex>` value (a small shared helper function, e.g.
`app.prompt_hash.hash_prompt(text: str) -> str`, rather than duplicating the hashlib call in
both modules); the fixed-vector, determinism, one-character-change, unrelated-config, and
independent-identity tests from section 6, actually written this time; and either the
experiment's resolution or the explicit safety proof from section 7 as its gate. Until that
tier exists and is reviewed on its own, `PROMPT_VERSION` remains exactly what it is today: a
hand-maintained `"1"` for both agents.
