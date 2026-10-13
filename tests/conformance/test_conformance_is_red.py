"""Positive control (plan §7): every check must go red on the broken store.

A check that never failed has proven nothing. Each defect is switched on alone and must
turn red EXACTLY the checks `DEFECTS` names: a missing red means the check is blind to
its defect, an extra red means a check fails for a reason it does not own.
"""

import pytest
from broken_store.store import DEFECTS, build, config

from shopmcp.conformance.checks import CHECKS, Expect, run
from shopmcp.conformance.runner import serve

ALL = {cid for cid, _, _ in CHECKS}


def red(defect: str | None) -> dict[str, str]:
    with serve(build(defect)) as base:
        results = run(f"{base}/mcp", Expect.from_config(config()), local=True)
    return {r.id: r.detail for r in results if r.status != "ok"}


def test_every_check_has_a_defect_that_turns_it_red():
    assert set().union(*DEFECTS.values()) == ALL


def test_the_clean_broken_store_is_green_with_nothing_skipped():
    # Without a defect the same store passes everything, C13 and C14 included: a red
    # below is the defect's doing, not the harness's.
    assert red(None) == {}


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_defect_turns_exactly_its_checks_red(defect):
    got = red(defect)
    assert set(got) == DEFECTS[defect], got
