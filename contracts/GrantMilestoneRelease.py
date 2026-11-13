# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""Grant milestone release.

A funder locks GEN against a set of milestones. Each milestone carries a
frozen checklist of acceptance criteria and a fixed payout. The grantee
submits evidence (a URL); validator consensus then judges the evidence
against the frozen criteria, and funds move only when the agreed verdict
says every criterion is met.

Consensus replaces a single reviewer because funder and grantee disagree
by construction about whether work is done. Criteria are frozen at
creation, evidence is fetched on chain by both sides of the round, and
the payout is a deterministic state transition derived from the agreed
per-criterion verdict. Neither party can move funds by asserting
something; the verdict has to survive independent re-execution.

State machine, per milestone:

    PENDING -> SUBMITTED -> UNDER_REVIEW -> APPROVED -> PAID
                          -> REJECTED -> APPEALED -> UNDER_REVIEW
                                       -> APPROVED -> PAID
                                       -> REJECTED_FINAL

UNDER_REVIEW is the phase a milestone is in while review_milestone runs.
It never persists as stored state: consensus and settlement happen
atomically inside one transaction, so an observer only ever sees the
states before and after the call. A rejected milestone can be appealed
exactly once, with new evidence; a second rejection is final. After the
grant deadline the funder can reclaim whatever was never released.

Money rules: amounts are atto GEN (1 GEN = 10^18). create_grant is
payable and the attached value must equal the sum of the milestone
amounts exactly, so the contract never holds unaccounted dust. A
milestone pays exactly once. Approval is computed by deterministic code
from the agreed per-criterion booleans; the model never outputs a funding
decision, and no model text is ever executed or treated as an instruction.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *

# Agreement band for the numeric quality score (0-100). Leader and
# validator each score the delivery independently; their scores must land
# within this many points of each other. Ten points absorbs ordinary
# phrasing-level variance in how two runs score the same evidence, while a
# genuinely different reading still breaks consensus. Approval never
# depends on the score: it is a deterministic function of the agreed
# per-criterion booleans (see review_milestone).
SCORE_TOLERANCE = u256(10)

# Upper bound on evidence text placed in the review prompt. Evidence pages
# can be arbitrarily long; the prompt must stay bounded so a review stays
# comparable across validators. Leader and validator apply the same
# truncation, so it cannot break equivalence.
MAX_EVIDENCE_CHARS = 12000

# Sanity bounds on grant shape: keep calldata and storage bounded and make
# an oversized grant fail fast at creation.
MAX_MILESTONES_PER_GRANT = u256(20)
MAX_CRITERIA_PER_MILESTONE = u256(20)

GRANT_ACTIVE = "ACTIVE"
GRANT_COMPLETED = "COMPLETED"
GRANT_CANCELLED = "CANCELLED"

MILESTONE_PENDING = "PENDING"
MILESTONE_SUBMITTED = "SUBMITTED"
MILESTONE_UNDER_REVIEW = "UNDER_REVIEW"
MILESTONE_APPROVED = "APPROVED"
MILESTONE_PAID = "PAID"
MILESTONE_REJECTED = "REJECTED"
MILESTONE_APPEALED = "APPEALED"
MILESTONE_REJECTED_FINAL = "REJECTED_FINAL"


@allow_storage
@dataclass
class CriterionResult:
    """One frozen criterion, judged against the submitted evidence."""

    criterion_index: u256
    met: bool
    note: str


@allow_storage
@dataclass
class Milestone:
    """One funded milestone, with its frozen criteria and verdict record."""

    grant_id: u256
    index: u256
    title: str
    amount: u256
    # Frozen at creation; nothing in this contract ever edits criteria
    # after the grant exists, so the review always judges the same
    # checklist the funder funded.
    criteria: DynArray[str]
    status: str
    evidence_url: str
    evidence_submitted_at: u256
    appeal_count: u256
    appeal_evidence_url: str
    # Verdict of the latest consensus round. verdict_approved is computed
    # by deterministic code from verdict_results, never copied from model
    # output. Score and confidence are recorded for transparency; neither
    # gates payment.
    verdict_approved: bool
    verdict_score: u256
    verdict_confidence: str
    verdict_rationale: str
    verdict_results: DynArray[CriterionResult]
    reviewed_at: u256


@allow_storage
@dataclass
class Grant:
    """A funded grant: who pays, who delivers, what is locked."""

    funder: Address
    grantee: Address
    title: str
    description: str
    total_locked: u256
    released: u256
    # Unix seconds. After this time the funder may cancel and reclaim the
    # unreleased remainder.
    deadline: u256
    status: str
    milestone_ids: DynArray[u256]


def _now_seconds() -> u256:
    """Current transaction time as unix seconds.

    The GenVM wires the standard library clock to the transaction datetime,
    so every validator observes the same value.
    """
    return u256(int(datetime.now(timezone.utc).timestamp()))


def _as_u256(value, label: str) -> u256:
    """Coerce one numeric argument to u256.

    Studio's write form serializes numbers as JSON strings, and the
    pinned py-genlayer's u256() is a pass-through (typing.NewType), so
    it does not convert them: u256("2000000000000000000") is still the
    string, and the first arithmetic on it crashes with
    TypeError: unsupported operand type(s) for +: 'int' and 'str'. int()
    normalizes both forms (a no-op on values that are already ints). A
    string that is not a whole number raises a clean [EXPECTED] revert
    here instead of letting a bare ValueError escape the contract.
    """
    try:
        return u256(int(value))
    except (TypeError, ValueError):
        raise gl.vm.UserError("[EXPECTED] " + label + " must be a whole number")


def _require_valid_url(url: str) -> None:
    if not (url.startswith("https://") or url.startswith("http://")):
                    raise gl.vm.UserError("[EXPECTED] evidence URL must start with http:// or https://")


def _fetch_evidence_text(url: str) -> str:
    """Fetch the evidence page for one review round.

    Runs inside the leader and inside the validator, so both sides observe
    the same URL. Failures carry a stable prefix: 429 and 5xx are
    [TRANSIENT] (worth retrying later), anything else is [EXTERNAL] (the
    URL itself is wrong). Messages are fixed strings, never built from
    fetched content, so both consensus sides raise the identical error.
    """
    response = gl.nondet.web.get(url)
    if response.status == 429 or response.status >= 500:
        raise gl.vm.UserError("[TRANSIENT] evidence host is temporarily unavailable")
    if response.status != 200:
        raise gl.vm.UserError("[EXTERNAL] evidence URL did not return HTTP 200")
    if response.body is None or len(response.body) == 0:
        raise gl.vm.UserError("[EXTERNAL] evidence URL returned an empty body")
    text = response.body.decode("utf-8", errors="replace")
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS]
    return text


def _build_review_prompt(
    milestone_title: str,
    criteria: list[str],
    evidence_text: str,
    evidence_url: str,
) -> str:
    """Build the exact prompt both consensus sides run.

    The prompt is a pure function of the frozen criteria and the fetched
    evidence, so leader and validator judge the same question. It asks for
    one verdict per criterion plus an overall quality score; it never asks
    for a funding decision, because the contract derives approval from the
    per-criterion booleans itself.
    """
    numbered = []
    for i in range(len(criteria)):
        numbered.append(str(i + 1) + ". " + criteria[i])
    criteria_block = "\n".join(numbered)
    return (
        "You are an impartial reviewer of grant milestone deliveries. "
        "Judge ONLY the evidence below against the frozen acceptance criteria. "
        "Do not use outside knowledge about the project, and do not infer work that is not shown in the evidence.\n"
        "\n"
        "Milestone: " + milestone_title + "\n"
        "Evidence source: " + evidence_url + "\n"
        "\n"
        "Frozen acceptance criteria:\n"
        + criteria_block
        + "\n\n"
        "Delivered evidence:\n"
        + evidence_text
        + "\n\n"
        "Return ONLY a JSON object with exactly these fields:\n"
        '{"results": [{"criterion_index": 0, "met": true, "note": "one short sentence pointing at the evidence"}], '
        '"score": 0, "confidence": "high", "rationale": "one or two sentences"}.\n'
        "Rules: results must contain one entry per criterion, in order, with criterion_index starting at 0. "
        "Set met to true only when the evidence clearly demonstrates that criterion; missing, vague, or "
        "unverifiable evidence means met is false. score is an integer from 0 to 100 for the overall quality "
        "of the delivery. confidence is high, medium, or low. rationale summarizes the judgement in one or two sentences."
    )


def _validate_verdict(verdict: dict, criteria_count: int) -> dict:
    """Check the model's verdict shape; return a normalized copy.

    Raises [LLM_ERROR] on any deviation: wrong top-level type, wrong number
    of per-criterion results, a non-boolean met flag, a score outside 0-100,
    or a confidence value outside the allowed band. Every message is a fixed
    string, never built from model output, so the error is identical on both
    consensus sides.
    """
    if not isinstance(verdict, dict):
        raise gl.vm.UserError("[LLM_ERROR] review verdict is not a JSON object")
    results = verdict.get("results")
    if not isinstance(results, list) or len(results) != criteria_count:
        raise gl.vm.UserError(
            "[LLM_ERROR] review verdict has the wrong number of criterion results"
        )
    clean_results = []
    for i in range(criteria_count):
        entry = results[i]
        if not isinstance(entry, dict):
            raise gl.vm.UserError("[LLM_ERROR] criterion result is not a JSON object")
        met = entry.get("met")
        if not isinstance(met, bool):
            raise gl.vm.UserError("[LLM_ERROR] criterion result has no boolean met flag")
        note = entry.get("note", "")
        if not isinstance(note, str):
            raise gl.vm.UserError("[LLM_ERROR] criterion result note is not a string")
        clean_results.append({"criterion_index": i, "met": met, "note": note})
    score = verdict.get("score")
    if (
        not isinstance(score, int)
        or isinstance(score, bool)
        or score < 0
        or score > 100
    ):
        raise gl.vm.UserError("[LLM_ERROR] review verdict score is not an integer in 0-100")
    confidence = verdict.get("confidence")
    if confidence not in ("high", "medium", "low"):
        raise gl.vm.UserError("[LLM_ERROR] review verdict confidence is not high, medium or low")
    rationale = verdict.get("rationale", "")
    if not isinstance(rationale, str):
        raise gl.vm.UserError("[LLM_ERROR] review verdict rationale is not a string")
    return {
        "results": clean_results,
        "score": score,
        "confidence": confidence,
        "rationale": rationale,
    }


def _judge_criteria(
    milestone_title: str,
    criteria: list[str],
    evidence_url: str,
) -> dict:
    """One full judgement round: fetch evidence, ask the model, validate.

    Shared by leader and validator so both sides run byte-identical logic.
    Returns per-criterion met/not_met, an overall 0-100 score, a confidence
    band, and a short rationale.
    """
    evidence_text = _fetch_evidence_text(evidence_url)
    prompt = _build_review_prompt(milestone_title, criteria, evidence_text, evidence_url)
    verdict = gl.nondet.exec_prompt(prompt, response_format="json")
    return _validate_verdict(verdict, len(criteria))


class GrantMilestoneRelease(gl.Contract):
    grant_counter: u256
    milestone_counter: u256
    grant_ids: DynArray[u256]
    grants: TreeMap[u256, Grant]
    milestones: TreeMap[u256, Milestone]

    def __init__(self) -> None:
        # Counters start at zero; the first grant and milestone are id 1.
        # Maps and lists start empty by default and need no initialization.
        self.grant_counter = u256(0)
        self.milestone_counter = u256(0)

    def _require_grant(self, grant_id: u256) -> Grant:
        if grant_id not in self.grants:
            raise gl.vm.UserError("[EXPECTED] grant does not exist")
        return self.grants[grant_id]

    def _milestone_id_for(self, grant: Grant, milestone_index: u256) -> u256:
        if milestone_index >= u256(len(grant.milestone_ids)):
            raise gl.vm.UserError("[EXPECTED] milestone index is out of range")
        return grant.milestone_ids[milestone_index]

    def _release_payment(self, grant: Grant, milestone: Milestone) -> None:
        """Move one milestone's payout to the grantee and update totals.

        Called only after consensus approved the milestone, from
        deterministic code. The transfer is emitted as a child message on
        finalization and carries exactly the milestone amount, never more.
        The grantee is expected to be an externally owned account; a
        contract grantee must accept plain value transfers.
        """
        amount = milestone.amount
        if grant.released + amount > grant.total_locked:
            raise gl.vm.UserError("[EXPECTED] milestone payout would exceed the locked total")
        grant.released = grant.released + amount
        gl.get_contract_at(grant.grantee).emit_transfer(value=amount)
        if grant.released == grant.total_locked:
            grant.status = GRANT_COMPLETED

    @gl.public.write.payable
    def create_grant(
        self,
        grantee: Address,
        title: str,
        description: str,
        deadline: u256,
        milestone_titles: DynArray[str],
        milestone_amounts: DynArray[u256],
        criteria_counts: DynArray[u256],
        criteria_text: DynArray[str],
    ) -> u256:
        """Create a grant and lock its full funding in the contract.

        The caller is the funder. Milestone criteria arrive as one flat list
        plus a per-milestone count, so the call stays a set of plain arrays:
        criteria_text holds every milestone's criteria in order, and
        criteria_counts[i] says how many belong to milestone i. The attached
        value must equal the sum of the milestone amounts exactly. Returns
        the new grant id.
        """
        # Coerce numeric arguments up front, before any arithmetic,
        # comparison, or storage (see _as_u256): Studio's write form can
        # deliver them as strings.
        deadline = _as_u256(deadline, "deadline")
        milestone_amounts = [_as_u256(a, "milestone amount") for a in milestone_amounts]
        criteria_counts = [_as_u256(c, "criteria count") for c in criteria_counts]
        if grantee == Address(bytes(20)):
            raise gl.vm.UserError("[EXPECTED] grantee must not be the zero address")
        if len(title) == 0:
            raise gl.vm.UserError("[EXPECTED] grant title must not be empty")
        milestone_count = len(milestone_titles)
        if milestone_count == 0 or u256(milestone_count) > MAX_MILESTONES_PER_GRANT:
            raise gl.vm.UserError("[EXPECTED] grant must have 1-20 milestones")
        if len(milestone_amounts) != milestone_count or len(criteria_counts) != milestone_count:
            raise gl.vm.UserError("[EXPECTED] milestone arrays must have the same length")
        now = _now_seconds()
        if deadline <= now:
            raise gl.vm.UserError("[EXPECTED] deadline must be in the future")
        total = u256(0)
        criteria_total = u256(0)
        for i in range(milestone_count):
            amount = u256(milestone_amounts[i])
            if amount == u256(0):
                raise gl.vm.UserError("[EXPECTED] every milestone amount must be greater than zero")
            total = total + amount
            count = u256(criteria_counts[i])
            if count == u256(0) or count > MAX_CRITERIA_PER_MILESTONE:
                raise gl.vm.UserError("[EXPECTED] every milestone must have 1-20 criteria")
            criteria_total = criteria_total + count
            if len(milestone_titles[i]) == 0:
                raise gl.vm.UserError("[EXPECTED] every milestone title must not be empty")
        if criteria_total != u256(len(criteria_text)):
            raise gl.vm.UserError("[EXPECTED] criteria text does not match criteria counts")
        if total != gl.message.value:
            raise gl.vm.UserError("[EXPECTED] locked value must equal the sum of milestone amounts")
        self.grant_counter = self.grant_counter + u256(1)
        grant_id = self.grant_counter
        milestone_ids: list = []
        criteria_offset = 0
        for i in range(milestone_count):
            self.milestone_counter = self.milestone_counter + u256(1)
            milestone_id = self.milestone_counter
            count = int(criteria_counts[i])
            criteria_slice = criteria_text[criteria_offset : criteria_offset + count]
            criteria_offset = criteria_offset + count
            self.milestones[milestone_id] = Milestone(
                grant_id=grant_id,
                index=u256(i),
                title=milestone_titles[i],
                amount=u256(milestone_amounts[i]),
                criteria=criteria_slice,
                status=MILESTONE_PENDING,
                evidence_url="",
                evidence_submitted_at=u256(0),
                appeal_count=u256(0),
                appeal_evidence_url="",
                verdict_approved=False,
                verdict_score=u256(0),
                verdict_confidence="",
                verdict_rationale="",
                verdict_results=[],
                reviewed_at=u256(0),
            )
            milestone_ids.append(milestone_id)
        self.grants[grant_id] = Grant(
            funder=gl.message.sender_address,
            grantee=grantee,
            title=title,
            description=description,
            total_locked=total,
            released=u256(0),
            deadline=deadline,
            status=GRANT_ACTIVE,
            milestone_ids=milestone_ids,
        )
        self.grant_ids.append(grant_id)
        return grant_id

    @gl.public.write
    def submit_evidence(
        self, grant_id: u256, milestone_index: u256, evidence_url: str
    ) -> None:
        """Submit the evidence URL for one milestone.

        Only the grantee may call this, and only while the milestone is
        PENDING. A rejected milestone takes new evidence through
        appeal_milestone instead, so a failed review cannot be overwritten
        by quietly resubmitting.
        """
        grant_id = _as_u256(grant_id, "grant id")
        milestone_index = _as_u256(milestone_index, "milestone index")
        grant = self._require_grant(grant_id)
        if grant.status != GRANT_ACTIVE:
            raise gl.vm.UserError("[EXPECTED] grant is not active")
        if gl.message.sender_address != grant.grantee:
            raise gl.vm.UserError("[EXPECTED] only the grantee can submit evidence")
        milestone_id = self._milestone_id_for(grant, milestone_index)
        milestone = self.milestones[milestone_id]
        if milestone.status != MILESTONE_PENDING:
            raise gl.vm.UserError("[EXPECTED] milestone is not awaiting evidence")
        _require_valid_url(evidence_url)
        milestone.evidence_url = evidence_url
        milestone.evidence_submitted_at = _now_seconds()
        milestone.status = MILESTONE_SUBMITTED

    @gl.public.write
    def review_milestone(self, grant_id: u256, milestone_index: u256) -> None:
        """Run validator consensus on one submitted milestone and settle it.

        Anyone may call this: the caller decides nothing, consensus does.
        The milestone must be SUBMITTED (first review) or APPEALED (review
        of an appeal). The leader fetches the evidence URL and judges each
        frozen criterion; the validator re-runs the same judgement and the
        round only finalizes when every criterion's met/not_met matches and
        the two quality scores land within SCORE_TOLERANCE. If consensus
        fails, the transaction does not finalize and the milestone stays
        SUBMITTED or APPEALED, so the review can be retried.

        After consensus, approval is computed by deterministic code from
        the agreed per-criterion booleans. An approved milestone pays out
        immediately; a rejected appeal is final.
        """
        grant_id = _as_u256(grant_id, "grant id")
        milestone_index = _as_u256(milestone_index, "milestone index")
        grant = self._require_grant(grant_id)
        if grant.status != GRANT_ACTIVE:
            raise gl.vm.UserError("[EXPECTED] grant is not active")
        milestone_id = self._milestone_id_for(grant, milestone_index)
        milestone = self.milestones[milestone_id]
        if milestone.status != MILESTONE_SUBMITTED and milestone.status != MILESTONE_APPEALED:
            raise gl.vm.UserError("[EXPECTED] milestone is not awaiting review")
        was_appeal = milestone.status == MILESTONE_APPEALED

        # Copy the milestone out of storage before the nondet block. The
        # leader and validator run in separate VMs and cannot touch storage;
        # they only ever see these plain values. Nothing here references
        # self, so the closures stay serializable.
        milestone_mem = gl.storage.copy_to_memory(milestone)
        evidence_url = milestone_mem.evidence_url
        milestone_title = milestone_mem.title
        criteria_list = [c for c in milestone_mem.criteria]

        def leader_fn() -> dict:
            return _judge_criteria(milestone_title, criteria_list, evidence_url)

        def validator_fn(result: gl.vm.Result) -> bool:
            # A leader that failed to produce a verdict (dead link,
            # malformed model output) is not something to agree with: the
            # review stays open and can be retried. Returning False keeps
            # the milestone in SUBMITTED/APPEALED instead of finalizing a
            # failure.
            if not isinstance(result, gl.vm.Return):
                return False
            leader_verdict = result.calldata
            try:
                own_verdict = _judge_criteria(milestone_title, criteria_list, evidence_url)
            except gl.vm.UserError:
                return False
            except Exception:
                # Fail closed: any unexpected validator-side failure counts
                # as disagreement, never as agreement.
                return False
            if not isinstance(leader_verdict, dict):
                return False
            leader_results = leader_verdict.get("results")
            if not isinstance(leader_results, list) or len(leader_results) != len(criteria_list):
                return False
            # Every criterion's met/not_met must match exactly. This is the
            # decision that moves funds, so there is no tolerance here.
            for i in range(len(criteria_list)):
                leader_entry = leader_results[i]
                if not isinstance(leader_entry, dict):
                    return False
                if own_verdict["results"][i]["met"] != leader_entry.get("met"):
                    return False
            # The overall score is a secondary signal: it must land within
            # the tolerance band, but it never decides approval on its own.
            # u256 subtraction cannot go negative, so compare both ways.
            leader_score = leader_verdict.get("score")
            if not isinstance(leader_score, int) or isinstance(leader_score, bool):
                return False
            own_score = u256(own_verdict["score"])
            leader_score_u = u256(int(leader_score))
            if own_score >= leader_score_u:
                gap = own_score - leader_score_u
            else:
                gap = leader_score_u - own_score
            return gap <= SCORE_TOLERANCE

        milestone.status = MILESTONE_UNDER_REVIEW
        verdict = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        # Consensus agreed. From here on everything is deterministic: the
        # verdict's per-criterion booleans decide the outcome, and no raw
        # model text is ever read as an instruction.
        approved = True
        for entry in verdict["results"]:
            if not bool(entry["met"]):
                approved = False
        milestone.verdict_approved = approved
        milestone.verdict_score = u256(int(verdict["score"]))
        milestone.verdict_confidence = str(verdict["confidence"])
        milestone.verdict_rationale = str(verdict["rationale"])
        milestone.verdict_results = [
            CriterionResult(
                criterion_index=u256(int(entry["criterion_index"])),
                met=bool(entry["met"]),
                note=str(entry["note"]),
            )
            for entry in verdict["results"]
        ]
        milestone.reviewed_at = _now_seconds()
        if approved:
            milestone.status = MILESTONE_APPROVED
            self._release_payment(grant, milestone)
            milestone.status = MILESTONE_PAID
        elif was_appeal:
            milestone.status = MILESTONE_REJECTED_FINAL
        else:
            milestone.status = MILESTONE_REJECTED

    @gl.public.write
    def appeal_milestone(
        self, grant_id: u256, milestone_index: u256, new_evidence_url: str
    ) -> None:
        """Appeal a rejected milestone once, with new evidence.

        Only the grantee may call this, and only while the milestone is
        REJECTED. The appeal replaces the evidence URL and moves the
        milestone to APPEALED, where review_milestone judges it again. A
        second rejection after an appeal is final: the milestone moves to
        REJECTED_FINAL and cannot be appealed again.
        """
        grant_id = _as_u256(grant_id, "grant id")
        milestone_index = _as_u256(milestone_index, "milestone index")
        grant = self._require_grant(grant_id)
        if grant.status != GRANT_ACTIVE:
            raise gl.vm.UserError("[EXPECTED] grant is not active")
        if gl.message.sender_address != grant.grantee:
            raise gl.vm.UserError("[EXPECTED] only the grantee can appeal a milestone")
        milestone_id = self._milestone_id_for(grant, milestone_index)
        milestone = self.milestones[milestone_id]
        if milestone.status != MILESTONE_REJECTED:
            raise gl.vm.UserError("[EXPECTED] only a rejected milestone can be appealed")
        if milestone.appeal_count != u256(0):
            raise gl.vm.UserError("[EXPECTED] this milestone has already used its one appeal")
        _require_valid_url(new_evidence_url)
        milestone.appeal_evidence_url = new_evidence_url
        milestone.evidence_url = new_evidence_url
        milestone.evidence_submitted_at = _now_seconds()
        milestone.appeal_count = milestone.appeal_count + u256(1)
        milestone.status = MILESTONE_APPEALED

    @gl.public.write
    def cancel_grant(self, grant_id: u256) -> None:
        """Cancel a grant after its deadline and refund the remainder.

        Only the funder may call this, and only while the grant is ACTIVE
        and the deadline has passed. The unreleased balance goes back to
        the funder in one transfer. Milestones already paid stay paid;
        milestones still open simply stop being reviewable.
        """
        grant_id = _as_u256(grant_id, "grant id")
        grant = self._require_grant(grant_id)
        if gl.message.sender_address != grant.funder:
            raise gl.vm.UserError("[EXPECTED] only the funder can cancel a grant")
        if grant.status != GRANT_ACTIVE:
            raise gl.vm.UserError("[EXPECTED] grant is not active")
        if _now_seconds() < grant.deadline:
            raise gl.vm.UserError("[EXPECTED] grant cannot be cancelled before its deadline")
        refund = grant.total_locked - grant.released
        grant.status = GRANT_CANCELLED
        if refund > u256(0):
            gl.get_contract_at(grant.funder).emit_transfer(value=refund)

    @gl.public.view
    def get_grant(self, grant_id: u256) -> Grant:
        grant_id = _as_u256(grant_id, "grant id")
        return self._require_grant(grant_id)

    @gl.public.view
    def get_milestone(self, grant_id: u256, milestone_index: u256) -> Milestone:
        grant_id = _as_u256(grant_id, "grant id")
        milestone_index = _as_u256(milestone_index, "milestone index")
        grant = self._require_grant(grant_id)
        return self.milestones[self._milestone_id_for(grant, milestone_index)]

    @gl.public.view
    def list_grants(self) -> DynArray[u256]:
        return self.grant_ids

    @gl.public.view
    def get_grant_milestones(self, grant_id: u256) -> DynArray[u256]:
        grant_id = _as_u256(grant_id, "grant id")
        return self._require_grant(grant_id).milestone_ids
