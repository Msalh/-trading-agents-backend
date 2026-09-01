# Macro v2 Sampling Protocol — v1 (specification only)

**Status: NOT ACTIVATED.** This document defines the rule a future tier would need to
implement before Macro v2 (`app/macro_agent_v2.py`) is ever auto-triggered for the purpose
of evaluating its real-world accuracy. It does not change any code path. Tier 3.44's
on-demand-only design (`POST /agents/macro-shadow-v2/run`, manually triggered) is
unaffected and remains the only way this schema runs today.

Written in response to the eighteenth external review's finding: ad hoc manual shadow
runs are fine for reviewing JSON quality and prompt behavior, but selecting *when* to run
Macro v2 by hand makes any later accuracy measurement meaningless — the sample is
self-selected. This protocol exists so that, if/when Macro v2 evaluation is greenlit, the
sampling rule is already locked and reviewable, not improvised after the fact.

**Activation is a separate, explicit decision.** Implementing this protocol in code (wiring
a sampling check into the live Macro v1 call path) is its own future tier, requiring its
own review of the actual diff, its own cost/behavior-change confirmation, and its own
commit. Writing this document does not authorize that tier.

## 1. Eligibility

The population a sample is drawn from: every **scheduler-triggered** live call to
`app.macro_agent.run_macro()` (i.e. every `POST /agents/macro/run` the scheduler itself
fires, per `SCHEDULER_INTERVALS_MINUTES`). Manually-triggered runs — a dashboard "Run"
button click, an ad hoc test call — are explicitly **excluded** from eligibility, the same
`auto_policy` vs `manual_dashboard` distinction `app/paper_trades.py` already draws for
trade provenance. Manual runs are real production LLM calls, but they are not
representative of the system's steady-state cadence, and including them would let someone
influence the sample by choosing when to click a button.

## 2. Cadence / sampling key

Deterministic, pre-committed, and **not wall-clock-based** — a fixed time-of-day or
day-of-week rule would correlate with market session structure and reintroduce exactly the
selection bias this protocol exists to remove.

Rule: maintain a persistent monotonic counter, `eligible_macro_calls_seen`, incremented by
exactly 1 on every eligible (scheduler-triggered) live Macro call, regardless of whether
that call is sampled. A call is **sampled** when
`eligible_macro_calls_seen % SAMPLING_N == 0`, evaluated at the moment eligibility is
determined — before the outcome of that Macro call (or any candidate it might feed into)
is known. `SAMPLING_N` is a single locked constant, chosen once before activation and never
changed based on interim results; a starting value of 5–10 is reasonable given the
scheduler's `macro` interval, but the exact number should be picked at activation time
based on how much shadow-call cost is acceptable, not tuned afterward.

(Alternative considered: a stable hash of each call's own identifier —
e.g. `hash(event_id or scheduler-tick timestamp) % SAMPLING_N == 0` — is equally
deterministic and would work as well. The counter is preferred for this v1 because it's
simpler to persist, audit, and reason about; either satisfies the "no wall-clock, no
post-hoc choice" requirement and the choice between them should be locked, not left open,
before activation.)

## 3. Concurrency with the candidate

A sampled shadow call must run **at (or immediately adjacent to) the same time as** the
live Macro v1 call it is paired with — never triggered later against a candidate chosen
after the fact. This is what makes `run_macro_shadow_v2`'s optional `candidate_id`
parameter safe to use under this protocol (unlike today's manual on-demand use, where
linking to an old candidate creates a temporal mismatch, per the seventeenth review):
under activation, the shadow call fires in the same code path as the sampled live call,
so both `MacroOpinion.timestamp` (v1) and `MacroShadowOpinionV2.timestamp` (v2) land
within the same scheduler tick.

## 4. Failure handling

If the shadow call itself fails (network error, malformed JSON response, missing
`ANTHROPIC_API_KEY`, or any `MacroAgentV2Error`), it is logged exactly like any other LLM
call via the existing `track_llm_call("macro_shadow_v2", ...)` telemetry (success=0), and
**no row is written** to `macro_shadow_opinions_v2` for that sampled slot — no partial or
error placeholder record. The sampled slot is simply missing from the eventual dataset,
not retried. Retrying would run the shadow call at a different, later moment than the
paired live call, reintroducing the exact temporal mismatch section 3 exists to prevent.
`eligible_macro_calls_seen` still increments normally regardless of shadow success/failure
— cadence is driven by eligible LIVE calls, not by shadow-call outcomes, so a shadow
failure never skews which future calls get sampled.

The eventual attrition rate (`sampled slots` vs `slots with a successfully stored shadow
opinion`) must be reported alongside any accuracy evaluation, not silently dropped from
the denominator.

## 5. Deduplication

Each eligible live call is evaluated for sampling exactly once, at the moment
`eligible_macro_calls_seen` is incremented — a strictly sequential counter makes
double-counting structurally impossible as long as the increment and the sampling check
happen atomically in the same code path as the live call itself (not in a separate,
re-runnable process). A sampled call gets exactly one shadow attempt (see section 4 — no
automatic retry). If the live Macro call path is ever re-entered for the same logical
event (a retry, a duplicate webhook), that must not increment the counter a second time
for what is really the same eligible call — the future implementation needs its own
idempotency key for this (e.g. keyed off the same `event_id`/anchor timestamp the rest of
the pipeline already uses for causal-integrity anchoring), not invented fresh here.

## 6. Comparison target

What a collected shadow observation is eventually evaluated against — this needs to be
locked before the first formal sample is collected, not decided retroactively once data
exists:

- **Qualitative agreement**: does v2's `directional_bias`/`tradeability` agree with v1's
  `direction`/`flags` from the *same* paired live call? This says something about whether
  the two schemas see the same situation similarly, not about either being "right."
- **Realized-outcome accuracy**: does v2's `directional_bias` (and/or `tradeability`)
  predict which way price actually moved over some forward horizon, using the same
  realized-outcome machinery `app/outcomes.py` already provides for other agents? This is
  the actual "is this schema useful" question, and is almost certainly the one that
  matters if Macro v2 is ever meant to inform real decisions.

Both may be worth tracking, but which one (or both) constitutes a formal "evaluation" for
deciding whether Macro v2 should ever influence anything must be fixed here, before
activation — not chosen after seeing which one looks better.

## 7. Evaluation window (no early peeking, same discipline as the prospective experiment)

A minimum sample size AND a minimum calendar-day span must both be satisfied before any
qualitative-agreement or realized-outcome read is treated as meaningful — mirroring the
prospective experiment's own `min_distinct_trading_days`/count-based stopping rule
(Tier 3.43), including reporting counts/progress only until the window is satisfied, never
partial accuracy numbers along the way. Exact thresholds are an activation-time decision
(dependent on the chosen `SAMPLING_N` and how much attrition section 4 produces), not
specified here.

## Summary — what activation would require

A future tier implementing this protocol would need, at minimum: the
`eligible_macro_calls_seen` counter (new storage), the sampling check wired into the live
scheduler-triggered Macro call path (not the manual-run path), a locked `SAMPLING_N`, the
shadow call fired synchronously alongside the sampled live call with `candidate_id`
populated per section 3, the no-retry/no-partial-row failure handling of section 4, and an
explicit choice of comparison target(s) and evaluation-window thresholds. Until that tier
exists and is reviewed on its own, Macro v2 remains exactly what it is today: an
on-demand, manually-triggered tool for qualitative schema/prompt review only.
