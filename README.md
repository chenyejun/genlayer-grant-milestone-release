# Grant Milestone Release

A GenLayer Intelligent Contract that releases grant funding per milestone,
only when validator consensus judges the delivered evidence against
acceptance criteria frozen at creation.

## The problem

Grants are usually enforced by trust: the funder decides whether the work
is done, or a single reviewer does. Both fail in the obvious ways. A
funder can stall payment for finished work; a grantee can claim payment
for unfinished work; a single reviewer can be wrong, slow, or captured.
Disputes end up in chat threads, not in verifiable records.

This contract replaces the single decider with a rule both sides agree to
up front: the criteria are frozen when the money is locked, the evidence
is fetched on chain by both sides of a consensus round, and the payout is
a deterministic state transition derived from the agreed verdict. Neither
party can move funds by asserting something.

## How it works

1. **Create.** The funder calls `create_grant` with the grantee's address,
   a deadline, and one entry per milestone: a title, a payout amount, and
   a checklist of acceptance criteria. The call is payable and the
   attached GEN must equal the sum of the milestone amounts exactly, so
   the contract never holds unaccounted dust. Criteria are frozen at this
   point; nothing in the contract can edit them afterwards.
2. **Submit.** The grantee calls `submit_evidence` with a URL pointing at
   the delivery (a report, a repository, a demo page). Only the grantee
   can submit, and only while the milestone is pending.
3. **Review.** Anyone calls `review_milestone`. The leader fetches the
   evidence URL and judges each frozen criterion; the validator re-runs
   the same judgement. The round finalizes only if every criterion's
   met/not_met matches and the two quality scores land within the
   tolerance band (see below). On agreement, deterministic code computes
   approval from the per-criterion booleans and, if approved, pays the
   milestone amount to the grantee immediately. The full verdict —
   per-criterion results, score, confidence band, rationale — is stored on
   chain with the milestone.
4. **Appeal.** A rejected milestone can be appealed once, with a new
   evidence URL, and is reviewed again. A second rejection is final.
5. **Cancel.** After the deadline, the funder can cancel the grant and
   reclaim whatever was never released. Milestones already paid stay paid.

## State design

Two maps hold all state; counters are the only scalars.

| Storage | Type | Contents |
|---|---|---|
| `grants` | `TreeMap[u256, Grant]` | Funder, grantee, title, description, `total_locked`, `released`, deadline, status, milestone ids |
| `milestones` | `TreeMap[u256, Milestone]` | Grant id, index, title, amount, frozen `criteria`, status, evidence URL and submission time, appeal count and appeal evidence URL, verdict record, review time |
| `grant_ids` | `DynArray[u256]` | Grant ids in creation order, for `list_grants` |
| `grant_counter`, `milestone_counter` | `u256` | Id sequences; the first grant and milestone are id 1 |

Grant status: `ACTIVE` → `COMPLETED` (last milestone paid) or `CANCELLED`
(funder cancels after the deadline). Milestone status:

```
PENDING -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> PAID
                     -> REJECTED -> APPEALED -> UNDER_REVIEW
                                  -> APPROVED -> PAID
                                  -> REJECTED_FINAL
```

`UNDER_REVIEW` is the phase a milestone is in while `review_milestone`
runs. It never persists as stored state: consensus and settlement happen
atomically inside one transaction, so an observer only ever sees the
states before and after the call. A rejected milestone cannot be
resubmitted through `submit_evidence`; the only way back is the single
appeal, which keeps the history (first verdict, appeal evidence, second
verdict) readable on chain.

Amounts are atto GEN (1 GEN = 10^18) and every amount is a `u256`. The
contract holds exactly `total_locked - released` per active grant, and a
milestone can pay at most once: review requires `SUBMITTED` or `APPEALED`,
and a paid milestone can never return to those states.

## How the validator / equivalence check works

`review_milestone` uses `gl.vm.run_nondet_unsafe(leader_fn,
validator_fn)` with positional arguments only. Before the nondet block,
the milestone record is copied out of storage with
`gl.storage.copy_to_memory` and only plain values (evidence URL, title,
criteria list) are captured by the closures; the closures never reference
`self`, so they stay serializable across validator VMs.

Both sides run the same function, `_judge_criteria`:

1. Fetch the evidence URL with `gl.nondet.web.get`. Non-200, empty, and
   unreachable responses fail with a fixed-prefix `gl.vm.UserError`
   (`[EXTERNAL]` for a wrong URL, `[TRANSIENT]` for a 429/5xx host), so a
   failed fetch is a clean, retryable failure rather than a verdict.
2. Build the review prompt deterministically from the frozen criteria and
   the fetched evidence (truncated to 12,000 characters, identically on
   both sides), and call `gl.nondet.exec_prompt` with
   `response_format="json"`.
3. Validate the model's verdict shape with `_validate_verdict`: exactly
   one result per criterion, a boolean `met` per result, a score in
   0–100, a confidence band of `high`/`medium`/`low`. Anything else raises
   `[LLM_ERROR]` with a fixed message.

The validator then requires two things:

- **Per-criterion agreement, exact.** Every criterion's `met`/`not_met`
  must match between leader and validator. This is the decision that
  moves funds, so there is no tolerance here. Free-text rationale and
  notes are recorded but never compared: two runs can phrase the same
  judgement differently, and only the booleans decide the outcome.
- **Score within tolerance.** The overall quality scores (0–100) must land
  within **±10 points** of each other.

### Why a 10-point band

The score is a secondary signal, not the decision. Two independent runs
scoring the same delivery routinely differ by a few points on phrasing and
emphasis alone; a band narrower than that would fail consensus on
deliveries both sides actually agree about, and every failed round costs
the grantee another review cycle. Ten points on a 100-point scale absorbs
that ordinary variance while still catching a genuinely different reading
— a run that scores 90 where the other scores 50 is not describing the
same delivery. The band is deliberately asymmetric in role: it can veto a
round (a large gap means disagreement), but it can never approve one,
because approval is computed from the per-criterion booleans alone. The
tolerance is a module constant, `SCORE_TOLERANCE`, so a deployer who
wants a stricter or looser band changes one line and redeploys.

No `strict_eq` is used anywhere on model output. The leader's verdict is
compared field by field, and the deterministic code after consensus
recomputes approval from the agreed booleans (`all(met)`) instead of
trusting any approval flag the model might emit — the verdict dict the
leader returns does not even contain an approval field.

## Failure paths

All failures raise `gl.vm.UserError` with a stable prefix; no bare
exceptions escape the contract.

| Prefix | Meaning | Examples |
|---|---|---|
| `[EXPECTED]` | Caller, state, or input error. Deterministic. | Unknown grant, wrong caller (only grantee submits/appeals, only funder cancels), milestone in the wrong state, mismatched locked value, past deadline, bad URL, payout exceeding the locked total |
| `[EXTERNAL]` | The evidence URL is wrong. Retrying the same URL will not help. | Non-200 response, empty body |
| `[TRANSIENT]` | The evidence host is temporarily unavailable. Retry the review later. | HTTP 429, HTTP 5xx |
| `[LLM_ERROR]` | The model's verdict was malformed. Retry the review. | Not a JSON object, wrong number of criterion results, non-boolean `met`, score outside 0–100, confidence outside the allowed band |

If consensus fails (validator disagrees), the transaction does not
finalize: the milestone stays `SUBMITTED` or `APPEALED` and the review can
be retried. If the leader fails to produce a verdict at all, the
validator returns `False` rather than agreeing with a failure, which has
the same effect.

Known limits:

- Evidence must be fetchable by a plain GET and readable as text. Pages
  that render only in JavaScript will return little or no content; point
  evidence at a rendered document, a repository, or a static page.
- The evidence page is truncated to 12,000 characters for the prompt. Put
  the decisive material early, or link to a concise report.
- The grantee is expected to be an externally owned account. A contract
  grantee must accept plain value transfers; a failed child transfer is
  not automatically refunded by the GenVM. The same applies to the
  funder's refund on cancel.
- The review prompt and the evidence are public to validators, by design:
  do not submit evidence containing secrets.

## Methods

| Method | Type | Who | What |
|---|---|---|---|
| `create_grant(grantee, title, description, deadline, milestone_titles, milestone_amounts, criteria_counts, criteria_text)` | write, payable | funder | Create a grant, lock funds, freeze criteria. Returns grant id |
| `submit_evidence(grant_id, milestone_index, evidence_url)` | write | grantee | Submit evidence for a pending milestone |
| `review_milestone(grant_id, milestone_index)` | write | anyone | Run consensus and settle the milestone |
| `appeal_milestone(grant_id, milestone_index, new_evidence_url)` | write | grantee | Appeal a rejected milestone once, with new evidence |
| `cancel_grant(grant_id)` | write | funder | Cancel after the deadline, refund the remainder |
| `get_grant(grant_id)` | view | anyone | Full grant record |
| `get_milestone(grant_id, milestone_index)` | view | anyone | Full milestone record, including the verdict |
| `list_grants()` | view | anyone | Grant ids in creation order |
| `get_grant_milestones(grant_id)` | view | anyone | Milestone ids of a grant |

`create_grant` takes criteria as one flat list plus a per-milestone count:
`criteria_text` holds every milestone's criteria in order, and
`criteria_counts[i]` says how many belong to milestone `i`. Example: two
milestones with 2 and 1 criteria pass `criteria_counts = [2, 1]` and
`criteria_text = ["Code is public", "Tests pass", "Docs updated"]`.
Milestone amounts are atto GEN; the attached value must equal their sum.
`deadline` is unix seconds.

## Deploy on StudioNet

1. Open https://studio.genlayer.com/contracts and connect a wallet funded
   with StudioNet GEN (testnet funds only).
2. Create a new contract, paste `contracts/GrantMilestoneRelease.py`, and
   deploy. The constructor takes **no arguments**, so leave the
   constructor fields empty.
3. Note the deployed address from the explorer link Studio shows.
4. Follow `DEPLOY_NOTES.md` to exercise every write method once and
   confirm the expected state after each step.

## Tests

Direct-mode tests live in `tests/direct/test_grant_milestone_release.py`
and run with the `genlayer-test` package:

```bash
pip install genlayer-test
pytest tests/direct/ -v
```

They run the contract in-process with mocked web and LLM responses, so no
network or funds are needed. Coverage: grant creation and input
validation, evidence submission permissions, approval paying both
milestones to completion, rejection, validator agreement and disagreement
(per-criterion and score-band), appeal then approval, second rejection
final, cancellation after the deadline, unreachable evidence (`[EXTERNAL]`
and `[TRANSIENT]`), malformed model verdicts (`[LLM_ERROR]`), and unknown
ids. Two direct-mode limits apply: value movement is not simulated, so
payment is asserted through on-chain state rather than balances; and only
the leader runs, so disagreement is tested explicitly with
`vm.run_validator()`.

## Repository layout

```
grant-milestone-release/
  contracts/GrantMilestoneRelease.py   the contract
  tests/direct/test_grant_milestone_release.py
  README.md                            this file
  DEPLOY_NOTES.md                      constructor args and the Studio checklist
```
