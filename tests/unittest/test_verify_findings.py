"""Unit tests for the pr_reviewer.verify_findings second-pass gate."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer

from ._settings_helpers import restore_settings, snapshot_settings

PREDICTION = """\
review:
  estimated_effort_to_review_[1-5]: 3
  key_issues_to_review:
    - relevant_file: src/a.py
      issue_header: Possible Bug
      issue_content: first issue
      start_line: 1
      end_line: 2
    - relevant_file: src/b.py
      issue_header: Possible Bug
      issue_content: second issue
      start_line: 3
      end_line: 4
"""

VERDICTS_DROP_SECOND = (
    "verdicts:\n"
    "  - issue: 1\n"
    "    supported: true\n"
    "    reason: present in diff\n"
    "  - issue: 2\n"
    "    supported: false\n"
    "    reason: code not in diff\n"
)


def _reviewer(verdicts: str) -> PRReviewer:
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.prediction = PREDICTION
    reviewer.prediction_data = None
    reviewer.patches_diff = "diff --git a/src/a.py b/src/a.py\n+new\n"
    reviewer.ai_handler = MagicMock()
    reviewer.ai_handler.chat_completion = AsyncMock(return_value=(verdicts, "stop"))
    return reviewer


@pytest.fixture(autouse=True)
def verify_flag():
    snapshot = snapshot_settings(["pr_reviewer.verify_findings"])
    get_settings().set("pr_reviewer.verify_findings", True)
    yield
    restore_settings(snapshot)


async def test_unsupported_findings_are_dropped() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert len(issues) == 1
    assert issues[0]["relevant_file"] == "src/a.py"


async def test_gate_disabled_skips_model_call() -> None:
    get_settings().set("pr_reviewer.verify_findings", False)
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    await reviewer._verify_key_issues()
    reviewer.ai_handler.chat_completion.assert_not_called()
    assert reviewer.prediction_data is None


async def test_model_error_keeps_original_findings() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.ai_handler.chat_completion = AsyncMock(side_effect=RuntimeError("boom"))
    await reviewer._verify_key_issues()
    assert reviewer.prediction_data is None


async def test_verdict_without_list_keeps_all_findings() -> None:
    reviewer = _reviewer("verdicts: none\n")
    await reviewer._verify_key_issues()
    assert reviewer.prediction_data is None


async def test_missing_verdict_keeps_the_issue() -> None:
    reviewer = _reviewer("verdicts:\n  - issue: 1\n    supported: true\n")
    await reviewer._verify_key_issues()
    # No drop => prediction_data untouched, downstream uses the original prediction.
    assert reviewer.prediction_data is None


async def test_middle_missing_verdict_keeps_the_issue() -> None:
    # Verdict for issue 2 skipped entirely: only the explicit refute of 3 may drop.
    reviewer = _reviewer(
        "verdicts:\n"
        "  - issue: 1\n    supported: true\n"
        "  - issue: 3\n    supported: false\n"
    )
    reviewer.prediction = PREDICTION.replace(
        "    - relevant_file: src/b.py\n",
        "    - relevant_file: src/b.py\n"
        "      issue_header: Bug\n      issue_content: second\n"
        "      start_line: 3\n      end_line: 4\n"
        "    - relevant_file: src/c.py\n"
        "      issue_header: Bug\n      issue_content: third\n"
        "      start_line: 5\n      end_line: 6\n",
        1,
    )
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in issues] == ["src/a.py", "src/b.py"]


async def test_malformed_verdict_entry_is_ignored() -> None:
    reviewer = _reviewer(
        "verdicts:\n"
        "  - issue: 1\n    supported: true\n"
        "  - garbage\n"
        "  - issue: 2\n    supported: false\n"
    )
    await reviewer._verify_key_issues()
    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert [i["relevant_file"] for i in issues] == ["src/a.py"]


async def test_empty_issue_list_skips_model_call() -> None:
    reviewer = _reviewer(VERDICTS_DROP_SECOND)
    reviewer.prediction = "review:\n  key_issues_to_review: []\n"
    await reviewer._verify_key_issues()
    reviewer.ai_handler.chat_completion.assert_not_called()
