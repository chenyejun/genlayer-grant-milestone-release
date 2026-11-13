# Deploy notes — Grant Milestone Release

## Constructor

The constructor takes **no arguments**. Deploy with the constructor
fields empty. The first grant created will be id 1.

Target network: **StudioNet** (GenLayer testnet), via
https://studio.genlayer.com/contracts. Use testnet GEN only.

## Example grant for the checklist

Use a small grant so every step is cheap to verify. Amounts are atto GEN
(1 GEN = 10^18).

- Grantee: any address you control (a second wallet, so you can see the
  payout arrive).
- Title: `Docs grant`
- Description: `Two documentation milestones.`
- Deadline: a unix timestamp about 7 days out (e.g. `1796976000` is
  2026-12-13T00:00:00Z; compute the value for the day you deploy).
- Milestone titles: `["Outline", "Full guide"]`
- Milestone amounts: `["2000000000000000000", "3000000000000000000"]`
  (2 GEN + 3 GEN = 5 GEN total)
- Criteria counts: `[1, 2]`
- Criteria text: `["Outline is public", "Guide is public", "Guide covers install and usage"]`
- Value attached to `create_grant`: **5 GEN exactly**.

Evidence URLs must be public pages fetchable by a plain GET. For the
checklist, use two real pages you control (a gist, a public repo file, a
static page). The first should satisfy the milestone's criteria; the
second should satisfy them after the appeal step, or use the same page
twice with different content.

## Checklist

Run the steps in order, from the funder wallet unless stated otherwise.
After each step, check the state with the view noted.

### 1. `create_grant` — happy path

Call with the example values above and 5 GEN attached.

- Expected: returns grant id `1`.
- Verify with `get_grant(1)`: `funder` is your wallet, `grantee` is the
  second wallet, `total_locked` is `5000000000000000000`, `released` is
  `0`, `status` is `ACTIVE`, `milestone_ids` is `[1, 2]`.
- Verify with `get_milestone(1, 0)`: `title` is `Outline`, `amount` is
  `2000000000000000000`, `criteria` is `["Outline is public"]`, `status`
  is `PENDING`.
- Verify with `get_milestone(1, 1)`: `criteria` is
  `["Guide is public", "Guide covers install and usage"]`.
- Verify with `list_grants()`: `[1]`.

### 2. `create_grant` — rejection cases (optional, costs gas each)

- Value 4 GEN instead of 5: reverts with
  `[EXPECTED] locked value must equal the sum of milestone amounts`.
- Deadline in the past: reverts with
  `[EXPECTED] deadline must be in the future`.
- A zero milestone amount: reverts with
  `[EXPECTED] every milestone amount must be greater than zero`.

### 3. `submit_evidence` — grantee submits for milestone 0

Call from the **grantee** wallet: `submit_evidence(1, 0, <evidence URL>)`.

- Expected: succeeds.
- Verify with `get_milestone(1, 0)`: `status` is `SUBMITTED`,
  `evidence_url` is the URL you passed, `evidence_submitted_at` is a
  recent unix timestamp.
- Negative check, from any other wallet: reverts with
  `[EXPECTED] only the grantee can submit evidence`.
- Negative check, URL `ftp://...`: reverts with
  `[EXPECTED] evidence URL must start with http:// or https://`.

### 4. `review_milestone` — approve milestone 0 and get paid

Call `review_milestone(1, 0)` from any wallet. The evidence page must
clearly satisfy the milestone's criterion ("Outline is public").

- Expected: succeeds. This is the consensus call, so it takes longer
  than the other writes.
- Verify with `get_milestone(1, 0)`: `status` is `PAID`,
  `verdict_approved` is `true`, `verdict_results[0].met` is `true`,
  `verdict_score` is a number in 0–100, `verdict_confidence` is
  `high`, `medium`, or `low`, `reviewed_at` is recent.
- Verify with `get_grant(1)`: `released` is `2000000000000000000`,
  `status` is still `ACTIVE`.
- Verify the **grantee wallet balance** increased by 2 GEN. (Direct-mode
  tests cannot check this; Studio is where the transfer is real.)
- If the review rejects instead: read `verdict_results` to see which
  criterion failed, improve the evidence page, and go to step 5 (appeal)
  rather than resubmitting — `submit_evidence` will revert while the
  milestone is `REJECTED`.

### 5. `appeal_milestone` — only needed if a milestone was rejected

Call from the **grantee** wallet, only while the milestone is `REJECTED`:
`appeal_milestone(1, <index>, <new evidence URL>)`.

- Expected: succeeds.
- Verify with `get_milestone(1, <index>)`: `status` is `APPEALED`,
  `appeal_count` is `1`, `evidence_url` is the new URL.
- Then call `review_milestone(1, <index>)` again and verify the new
  verdict the same way as step 4.
- If the appeal review rejects again, the milestone becomes
  `REJECTED_FINAL`: a further `appeal_milestone` reverts with
  `[EXPECTED] only a rejected milestone can be appealed`, and a further
  `review_milestone` reverts with
  `[EXPECTED] milestone is not awaiting review`.

### 6. `review_milestone` — approve milestone 1, grant completes

Submit evidence for milestone 1 from the grantee wallet, then review it.

- Expected: milestone 1 becomes `PAID`.
- Verify with `get_grant(1)`: `released` is `5000000000000000000`,
  `status` is `COMPLETED`.
- Verify the grantee balance increased by a further 3 GEN.

### 7. `cancel_grant` — refund path (use a second grant)

Create a second small grant (e.g. 1 GEN, one milestone, deadline a few
minutes out) and do not submit evidence for it.

- Before the deadline, `cancel_grant(2)` from the funder reverts with
  `[EXPECTED] grant cannot be cancelled before its deadline`.
- From any other wallet it reverts with
  `[EXPECTED] only the funder can cancel a grant`.
- After the deadline passes, `cancel_grant(2)` from the funder succeeds.
- Verify with `get_grant(2)`: `status` is `CANCELLED`, `released` is `0`.
- Verify the **funder wallet balance** increased by 1 GEN.
- After cancellation, `submit_evidence`, `review_milestone`, and
  `appeal_milestone` on that grant all revert with
  `[EXPECTED] grant is not active`.

## After deploy

- Record the deployed address and the explorer link for the submission.
- Keep the funder and grantee wallets funded with a little extra GEN for
  the review calls (consensus rounds cost more gas than plain writes).
- If a review comes back `UNDETERMINED` or keeps failing consensus on
  Studio, check the evidence page first: it must be reachable by plain
  GET and under 12,000 characters of decisive content. A page that renders
  only in JavaScript, or evidence split across many linked pages, is the
  usual cause.
