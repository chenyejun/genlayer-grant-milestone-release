"""Direct-mode tests for GrantMilestoneRelease.

Runs the contract in-process with mocked web and LLM responses
(gltest.direct). Two direct-mode limits shape what is asserted here:

- Value movement is NOT simulated: emit_transfer is a no-op in direct
  mode, so payment is asserted through on-chain state (milestone PAID,
  grant.released, grant COMPLETED) rather than balances. The Studio
  checklist in DEPLOY_NOTES.md covers verifying the real transfer after
  deploy.
- Only the leader of a consensus round runs; the validator is captured,
  not executed. Agreement and disagreement are tested explicitly with
  vm.run_validator() after (re)registering the LLM mock, which is how the
  validator's view of the evidence is simulated.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from gltest.direct import VMContext, create_address, deploy_contract

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "GrantMilestoneRelease.py"

ONE_GEN = 10**18
EVIDENCE_URL = "https://example.org/delivery/report.md"
EVIDENCE_URL_V2 = "https://example.org/delivery/report-v2.md"
EVIDENCE_BODY = (
    "# Delivery report\n\n"
    "## Code is public\n"
    "The repository is public and contains the full implementation.\n\n"
    "## Tests pass\n"
    "CI ran 42 tests, all passing.\n\n"
    "## Docs updated\n"
    "The README documents installation and usage.\n"
)
DEADLINE = int(datetime(2026, 12, 13, tzinfo=timezone.utc).timestamp())


def _verdict_json(results, score=85, confidence="high", rationale="Delivery satisfies all criteria."):
    return json.dumps(
        {
            "results": [
                {
                    "criterion_index": i,
                    "met": met,
                    "note": "Evidence shows criterion %d." % (i + 1),
                }
                for i, met in enumerate(results)
            ],
            "score": score,
            "confidence": confidence,
            "rationale": rationale,
        }
    )


def _setup():
    """Fresh VM warped to a fixed time."""
    vm = VMContext()
    vm.warp("2026-11-13T00:00:00Z")
    return vm


def _deploy(vm):
    """Deploy the contract and mint test addresses.

    Addresses must be created AFTER deploy: create_address imports
    Address from the genlayer SDK, which only becomes importable once
    deploy_contract has put the SDK on sys.path. Created earlier, it
    silently falls back to raw bytes, which the contract's Address
    storage rejects.
    """
    contract = deploy_contract(CONTRACT_PATH, vm)
    funder = create_address("funder")
    grantee = create_address("grantee")
    stranger = create_address("stranger")
    return contract, funder, grantee, stranger


def _create_grant(contract, vm, funder, grantee, deadline=DEADLINE):
    """Create the standard two-milestone grant (2 GEN + 3 GEN)."""
    amounts = [2 * ONE_GEN, 3 * ONE_GEN]
    vm.sender = funder
    vm.value = sum(amounts)
    grant_id = contract.create_grant(
        grantee,
        "Open source grant",
        "Fund two milestones of open source work.",
        deadline,
        ["Milestone one", "Milestone two"],
        amounts,
        [2, 1],
        ["Code is public", "Tests pass", "Docs updated"],
    )
    vm.value = 0
    return grant_id


def _submit(contract, vm, grantee, grant_id, index, url=EVIDENCE_URL):
    vm.sender = grantee
    contract.submit_evidence(grant_id, index, url)


def _mock_review(vm, results, score=85):
    """Register the evidence page and one LLM verdict for a review round."""
    vm.clear_mocks()
    vm.mock_web("example.org", {"status": 200, "body": EVIDENCE_BODY})
    vm.mock_llm("Frozen acceptance criteria", _verdict_json(results, score=score))


def test_create_grant_locks_funds_and_freezes_criteria():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        assert list(contract.list_grants()) == []
        grant_id = _create_grant(contract, vm, funder, grantee)
        assert grant_id == 1

        grant = contract.get_grant(grant_id)
        assert grant.funder == funder
        assert grant.grantee == grantee
        assert grant.total_locked == 5 * ONE_GEN
        assert grant.released == 0
        assert grant.status == "ACTIVE"
        assert list(grant.milestone_ids) == [1, 2]
        assert list(contract.get_grant_milestones(grant_id)) == [1, 2]
        assert list(contract.list_grants()) == [1]

        first = contract.get_milestone(grant_id, 0)
        assert first.title == "Milestone one"
        assert first.amount == 2 * ONE_GEN
        assert list(first.criteria) == ["Code is public", "Tests pass"]
        assert first.status == "PENDING"
        second = contract.get_milestone(grant_id, 1)
        assert list(second.criteria) == ["Docs updated"]
        assert second.amount == 3 * ONE_GEN


def test_create_grant_rejects_bad_inputs():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        vm.sender = funder

        # Past deadline.
        vm.value = 5 * ONE_GEN
        with vm.expect_revert("[EXPECTED] deadline must be in the future"):
            contract.create_grant(
                grantee, "G", "D", 1, ["M"], [5 * ONE_GEN], [1], ["C"]
            )

        # Attached value does not equal the milestone amounts.
        vm.value = 4 * ONE_GEN
        with vm.expect_revert("[EXPECTED] locked value must equal"):
            contract.create_grant(
                grantee, "G", "D", DEADLINE, ["M"], [5 * ONE_GEN], [1], ["C"]
            )

        # Zero milestone amount.
        vm.value = 3 * ONE_GEN
        with vm.expect_revert("[EXPECTED] every milestone amount must be greater than zero"):
            contract.create_grant(
                grantee, "G", "D", DEADLINE, ["M"], [0], [1], ["C"]
            )

        # Criteria text does not match the per-milestone counts.
        vm.value = 5 * ONE_GEN
        with vm.expect_revert("[EXPECTED] criteria text does not match criteria counts"):
            contract.create_grant(
                grantee, "G", "D", DEADLINE, ["M"], [5 * ONE_GEN], [2], ["C"]
            )
        vm.value = 0


def test_create_grant_accepts_string_numbers_from_studio_form():
    """Studio's write form sends numbers as JSON strings.

    The deployed contract crashed on this exact call on StudioNet with
    TypeError: unsupported operand type(s) for +: 'int' and 'str': the
    milestone amounts arrived as ["2000000000000000000",
    "3000000000000000000"], the criteria counts as ["2", "1"], and the
    deadline as "1796976000", and the pinned py-genlayer's u256() passes
    strings through unchanged. The contract coerces every numeric
    argument itself, so the same call must succeed and store integers.
    """
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        vm.sender = funder
        vm.value = 5 * ONE_GEN
        grant_id = contract.create_grant(
            grantee,
            "Open source grant",
            "Fund two milestones of open source work.",
            str(DEADLINE),
            ["Milestone one", "Milestone two"],
            [str(2 * ONE_GEN), str(3 * ONE_GEN)],
            ["2", "1"],
            ["Code is public", "Tests pass", "Docs updated"],
        )
        vm.value = 0
        assert grant_id == 1

        grant = contract.get_grant(grant_id)
        assert grant.total_locked == 5 * ONE_GEN
        assert grant.deadline == DEADLINE
        assert type(grant.total_locked) is int
        assert type(grant.deadline) is int

        first = contract.get_milestone(grant_id, 0)
        assert first.amount == 2 * ONE_GEN
        assert type(first.amount) is int
        assert list(first.criteria) == ["Code is public", "Tests pass"]
        second = contract.get_milestone(grant_id, 1)
        assert second.amount == 3 * ONE_GEN
        assert type(second.amount) is int
        assert list(second.criteria) == ["Docs updated"]


def test_create_grant_rejects_non_numeric_string_amount():
    """A string that is not a whole number fails cleanly.

    int() raises ValueError on "two"; the contract maps that to an
    [EXPECTED] revert instead of letting a bare exception escape.
    """
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        vm.sender = funder
        vm.value = 5 * ONE_GEN
        with vm.expect_revert("[EXPECTED] milestone amount must be a whole number"):
            contract.create_grant(
                grantee, "G", "D", DEADLINE, ["M"], ["two"], [1], ["C"]
            )
        vm.value = 0


def test_create_grant_accepts_hex_string_grantee():
    """Studio's write form sends the grantee as a hex string.

    The deployed contract crashed on this exact call on StudioNet with
    AttributeError: 'int' object has no attribute 'as_bytes' at the
    Grant record write: the form's address value arrives as a plain
    int, the zero-address check silently passes (an int never equals
    an Address), and AddrDesc.set then calls .as_bytes on the int.
    The contract coerces the grantee to a real Address itself, so the
    same call must succeed and store proper Address objects for both
    parties. The funder is gl.message.sender_address, already an
    Address at runtime; the stored funder is asserted here rather than
    converted.
    """
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        # Imported here, not at the top of the test: Address only
        # becomes importable once _deploy has put the SDK on sys.path.
        from genlayer.py.types import Address

        grant_id = _create_grant(contract, vm, funder, grantee.as_hex)
        assert grant_id == 1

        grant = contract.get_grant(grant_id)
        assert isinstance(grant.grantee, Address)
        assert isinstance(grant.funder, Address)
        assert grant.grantee == grantee
        assert grant.funder == funder
        assert grant.grantee.as_hex == grantee.as_hex
        assert grant.funder.as_hex == funder.as_hex


def test_create_grant_accepts_int_grantee():
    """The form's address value can also arrive as a plain int.

    Studio parses the typed hex string into an integer before the
    call reaches the contract. The contract reads that int as the
    standard big-endian hex-to-int value (the number int("0x...", 16)
    produces), so the stored grantee must be the same address that
    was typed in, not a byte-reversed one.
    """
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        # Imported here, not at the top of the test: Address only
        # becomes importable once _deploy has put the SDK on sys.path.
        from genlayer.py.types import Address

        grant_id = _create_grant(contract, vm, funder, int(grantee.as_hex, 16))
        assert grant_id == 1

        grant = contract.get_grant(grant_id)
        assert isinstance(grant.grantee, Address)
        assert grant.grantee == grantee
        assert grant.grantee.as_hex == grantee.as_hex
        assert grant.funder.as_hex == funder.as_hex


def test_create_grant_rejects_zero_address_grantee():
    """A zero grantee still reverts, in every form it can arrive in.

    Before the fix the check `grantee == Address(bytes(20))` silently
    passed for an int 0 or a zero hex string, because neither ever
    equals an Address object; the call then died later at the storage
    write instead of failing cleanly here.
    """
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        # Imported here, not at the top of the test: Address only
        # becomes importable once _deploy has put the SDK on sys.path.
        from genlayer.py.types import Address

        vm.sender = funder
        vm.value = 5 * ONE_GEN

        # Plain int zero, as the Studio form would deliver it.
        with vm.expect_revert("[EXPECTED] grantee must not be the zero address"):
            contract.create_grant(
                0, "G", "D", DEADLINE, ["M"], [5 * ONE_GEN], [1], ["C"]
            )

        # Zero as a hex string.
        with vm.expect_revert("[EXPECTED] grantee must not be the zero address"):
            contract.create_grant(
                "0x0000000000000000000000000000000000000000",
                "G",
                "D",
                DEADLINE,
                ["M"],
                [5 * ONE_GEN],
                [1],
                ["C"],
            )

        # Zero as a real Address object.
        with vm.expect_revert("[EXPECTED] grantee must not be the zero address"):
            contract.create_grant(
                Address(bytes(20)), "G", "D", DEADLINE, ["M"], [5 * ONE_GEN], [1], ["C"]
            )
        vm.value = 0


def test_string_ids_accepted_by_other_methods():
    """The same form serialization applies to grant and milestone ids.

    A Studio caller passes ids as strings too ("1", "0"); the contract
    coerces them at the entry of every method that takes one.
    """
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        assert contract.get_grant("1").total_locked == 5 * ONE_GEN
        assert list(contract.get_grant_milestones("1")) == [1, 2]
        assert contract.get_milestone("1", "0").status == "PENDING"

        _submit(contract, vm, grantee, "1", "0")
        assert contract.get_milestone(grant_id, 0).status == "SUBMITTED"


def test_submit_evidence_only_grantee_and_valid_url():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        vm.sender = stranger
        with vm.expect_revert("[EXPECTED] only the grantee can submit evidence"):
            contract.submit_evidence(grant_id, 0, EVIDENCE_URL)

        vm.sender = grantee
        with vm.expect_revert("[EXPECTED] evidence URL must start with http"):
            contract.submit_evidence(grant_id, 0, "ftp://example.org/report.md")

        _submit(contract, vm, grantee, grant_id, 0)
        milestone = contract.get_milestone(grant_id, 0)
        assert milestone.status == "SUBMITTED"
        assert milestone.evidence_url == EVIDENCE_URL
        assert milestone.evidence_submitted_at > 0

        with vm.expect_revert("[EXPECTED] milestone is not awaiting evidence"):
            contract.submit_evidence(grant_id, 0, EVIDENCE_URL)


def test_review_approves_and_pays_both_milestones():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)
        # Proves the leader/validator closures are serializable, which
        # production requires (they are pickled across validator VMs).
        vm.check_pickling = True

        _submit(contract, vm, grantee, grant_id, 0)
        _mock_review(vm, [True, True], score=85)
        contract.review_milestone(grant_id, 0)
        assert vm.run_validator() is True
        vm.clear_validators()

        first = contract.get_milestone(grant_id, 0)
        assert first.status == "PAID"
        assert first.verdict_approved is True
        assert first.verdict_score == 85
        assert first.verdict_confidence == "high"
        assert [r.met for r in first.verdict_results] == [True, True]
        assert first.reviewed_at > 0

        grant = contract.get_grant(grant_id)
        assert grant.released == 2 * ONE_GEN
        assert grant.status == "ACTIVE"

        _submit(contract, vm, grantee, grant_id, 1)
        _mock_review(vm, [True], score=90)
        contract.review_milestone(grant_id, 1)
        assert vm.run_validator() is True

        grant = contract.get_grant(grant_id)
        assert grant.released == 5 * ONE_GEN
        assert grant.status == "COMPLETED"
        assert contract.get_milestone(grant_id, 1).status == "PAID"


def test_review_rejects_when_a_criterion_is_unmet():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        _submit(contract, vm, grantee, grant_id, 0)
        _mock_review(vm, [True, False], score=60)
        contract.review_milestone(grant_id, 0)
        # The validator agrees with the rejection: same verdict.
        assert vm.run_validator() is True

        milestone = contract.get_milestone(grant_id, 0)
        assert milestone.status == "REJECTED"
        assert milestone.verdict_approved is False
        assert [r.met for r in milestone.verdict_results] == [True, False]
        assert contract.get_grant(grant_id).released == 0


def test_validator_disagrees_when_a_criterion_differs():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        _submit(contract, vm, grantee, grant_id, 0)
        _mock_review(vm, [True, True], score=85)
        contract.review_milestone(grant_id, 0)

        # The validator sees the second criterion as unmet.
        vm.clear_mocks()
        vm.mock_web("example.org", {"status": 200, "body": EVIDENCE_BODY})
        vm.mock_llm("Frozen acceptance criteria", _verdict_json([True, False], score=85))
        assert vm.run_validator() is False


def test_validator_disagrees_when_score_gap_exceeds_tolerance():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        _submit(contract, vm, grantee, grant_id, 0)
        _mock_review(vm, [True, True], score=90)
        contract.review_milestone(grant_id, 0)

        # Same per-criterion verdict, but a 40-point score gap (tolerance 10).
        vm.clear_mocks()
        vm.mock_web("example.org", {"status": 200, "body": EVIDENCE_BODY})
        vm.mock_llm("Frozen acceptance criteria", _verdict_json([True, True], score=50))
        assert vm.run_validator() is False


def test_validator_agrees_within_score_tolerance():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        _submit(contract, vm, grantee, grant_id, 0)
        _mock_review(vm, [True, True], score=85)
        contract.review_milestone(grant_id, 0)

        # Same per-criterion verdict, 5-point score gap (tolerance 10).
        vm.clear_mocks()
        vm.mock_web("example.org", {"status": 200, "body": EVIDENCE_BODY})
        vm.mock_llm("Frozen acceptance criteria", _verdict_json([True, True], score=80))
        assert vm.run_validator() is True


def test_appeal_then_approve():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        _submit(contract, vm, grantee, grant_id, 0)
        _mock_review(vm, [True, False], score=60)
        contract.review_milestone(grant_id, 0)
        assert contract.get_milestone(grant_id, 0).status == "REJECTED"

        vm.sender = stranger
        with vm.expect_revert("[EXPECTED] only the grantee can appeal"):
            contract.appeal_milestone(grant_id, 0, EVIDENCE_URL_V2)

        vm.sender = grantee
        with vm.expect_revert("[EXPECTED] evidence URL must start with http"):
            contract.appeal_milestone(grant_id, 0, "notaurl")

        contract.appeal_milestone(grant_id, 0, EVIDENCE_URL_V2)
        milestone = contract.get_milestone(grant_id, 0)
        assert milestone.status == "APPEALED"
        assert milestone.appeal_count == 1
        assert milestone.evidence_url == EVIDENCE_URL_V2
        assert milestone.appeal_evidence_url == EVIDENCE_URL_V2

        _mock_review(vm, [True, True], score=88)
        contract.review_milestone(grant_id, 0)
        assert contract.get_milestone(grant_id, 0).status == "PAID"

        with vm.expect_revert("[EXPECTED] only a rejected milestone can be appealed"):
            contract.appeal_milestone(grant_id, 0, EVIDENCE_URL_V2)


def test_second_rejection_is_final():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        _submit(contract, vm, grantee, grant_id, 0)
        _mock_review(vm, [False, False], score=20)
        contract.review_milestone(grant_id, 0)
        assert contract.get_milestone(grant_id, 0).status == "REJECTED"

        vm.sender = grantee
        contract.appeal_milestone(grant_id, 0, EVIDENCE_URL_V2)
        assert contract.get_milestone(grant_id, 0).status == "APPEALED"

        _mock_review(vm, [True, False], score=55)
        contract.review_milestone(grant_id, 0)
        assert contract.get_milestone(grant_id, 0).status == "REJECTED_FINAL"

        with vm.expect_revert("[EXPECTED] only a rejected milestone can be appealed"):
            contract.appeal_milestone(grant_id, 0, EVIDENCE_URL_V2)
        with vm.expect_revert("[EXPECTED] milestone is not awaiting review"):
            contract.review_milestone(grant_id, 0)


def test_cancel_grant_after_deadline():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        vm.sender = stranger
        with vm.expect_revert("[EXPECTED] only the funder can cancel"):
            contract.cancel_grant(grant_id)

        vm.sender = funder
        with vm.expect_revert("[EXPECTED] grant cannot be cancelled before its deadline"):
            contract.cancel_grant(grant_id)

        vm.warp("2026-12-14T00:00:00Z")
        contract.cancel_grant(grant_id)
        assert contract.get_grant(grant_id).status == "CANCELLED"

        vm.sender = grantee
        with vm.expect_revert("[EXPECTED] grant is not active"):
            contract.submit_evidence(grant_id, 0, EVIDENCE_URL)

        vm.sender = funder
        with vm.expect_revert("[EXPECTED] grant is not active"):
            contract.cancel_grant(grant_id)


def test_review_fails_cleanly_when_evidence_unreachable():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)
        _submit(contract, vm, grantee, grant_id, 0)

        # 404: the URL itself is wrong, no point retrying it.
        vm.clear_mocks()
        vm.mock_web("example.org", {"status": 404, "body": "not found"})
        vm.mock_llm("Frozen acceptance criteria", _verdict_json([True, True]))
        snap = vm.snapshot()
        with vm.expect_revert("[EXTERNAL]"):
            contract.review_milestone(grant_id, 0)
        vm.revert(snap)
        assert contract.get_milestone(grant_id, 0).status == "SUBMITTED"

        # 429: the host is throttling, worth retrying later.
        vm.clear_mocks()
        vm.mock_web("example.org", {"status": 429, "body": "slow down"})
        vm.mock_llm("Frozen acceptance criteria", _verdict_json([True, True]))
        snap = vm.snapshot()
        with vm.expect_revert("[TRANSIENT]"):
            contract.review_milestone(grant_id, 0)
        vm.revert(snap)
        assert contract.get_milestone(grant_id, 0).status == "SUBMITTED"


def test_review_fails_cleanly_when_llm_verdict_malformed():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)
        _submit(contract, vm, grantee, grant_id, 0)

        # Missing per-criterion results entirely.
        vm.clear_mocks()
        vm.mock_web("example.org", {"status": 200, "body": EVIDENCE_BODY})
        vm.mock_llm("Frozen acceptance criteria", json.dumps({"score": 95}))
        snap = vm.snapshot()
        with vm.expect_revert("[LLM_ERROR]"):
            contract.review_milestone(grant_id, 0)
        vm.revert(snap)
        assert contract.get_milestone(grant_id, 0).status == "SUBMITTED"

        # Score outside 0-100.
        vm.clear_mocks()
        vm.mock_web("example.org", {"status": 200, "body": EVIDENCE_BODY})
        vm.mock_llm(
            "Frozen acceptance criteria",
            _verdict_json([True, True], score=150),
        )
        snap = vm.snapshot()
        with vm.expect_revert("[LLM_ERROR]"):
            contract.review_milestone(grant_id, 0)
        vm.revert(snap)
        assert contract.get_milestone(grant_id, 0).status == "SUBMITTED"


def test_views_reject_unknown_ids():
    vm = _setup()
    with vm.activate():
        contract, funder, grantee, _stranger = _deploy(vm)
        grant_id = _create_grant(contract, vm, funder, grantee)

        with vm.expect_revert("[EXPECTED] grant does not exist"):
            contract.get_grant(999)
        with vm.expect_revert("[EXPECTED] milestone index is out of range"):
            contract.get_milestone(grant_id, 5)
        with vm.expect_revert("[EXPECTED] milestone index is out of range"):
            contract.review_milestone(grant_id, 5)
