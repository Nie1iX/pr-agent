"""Unit tests for gitlab.auto_resolve_fixed_inline_threads."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.gitlab_provider import (
    GitLabProvider,
    _is_fixed_own_inline_thread,
    _removed_lines_from_patch,
)

from ._settings_helpers import restore_settings, snapshot_settings

BOT_ID = 71
OLD_SHA = "a" * 40
HEAD_SHA = "b" * 40
AGENT_BODY = "**Suggestion:** drop the key <!-- pr-agent-dedup: aaaa1111bbbb -->"

PATCH = """\
@@ -1,3 +1,4 @@
 ctx
-old
+new one
+new two
 ctx
@@ -10,2 +10,3 @@
 ctx
+added
 ctx
"""

INSERT_ONLY_PATCH = """\
@@ -1,2 +1,3 @@
 ctx
+inserted
 ctx
"""


def _discussion(body=AGENT_BODY, path="app.py", line=4, head_sha=OLD_SHA,
                resolved=False, author_id=BOT_ID):
    note = {
        "resolved": resolved,
        "resolvable": True,
        "body": body,
        "author": {"id": author_id},
        "position": {
            "position_type": "text",
            "new_path": path,
            "new_line": line,
            "head_sha": head_sha,
        },
    }
    return SimpleNamespace(id="d1", attributes={"notes": [note]},
                           resolved=False, save=MagicMock())


def _provider(discussions, diffs) -> GitLabProvider:
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "owner/repo"
    provider.id_mr = 7
    provider.mr = MagicMock()
    provider.mr.diff_refs = {"head_sha": HEAD_SHA}
    provider.mr.discussions.list.return_value = discussions
    provider._get_own_user_id = MagicMock(return_value=BOT_ID)
    provider.gl = MagicMock()
    provider.gl.projects.get.return_value.repository_compare.return_value = {"diffs": diffs}
    return provider


@pytest.fixture(autouse=True)
def flag():
    snapshot = snapshot_settings(["gitlab.auto_resolve_fixed_inline_threads"])
    get_settings().set("gitlab.auto_resolve_fixed_inline_threads", True)
    yield
    restore_settings(snapshot)


def test_removed_lines_from_patch() -> None:
    assert _removed_lines_from_patch(PATCH) == {2}
    assert _removed_lines_from_patch(INSERT_ONLY_PATCH) == set()


def test_thread_on_removed_line_resolves() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.resolve_fixed_inline_threads()
    assert discussion.resolved is True
    discussion.save.assert_called_once()


def test_thread_on_untouched_line_stays_open() -> None:
    discussion = _discussion(line=3)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.resolve_fixed_inline_threads()
    assert discussion.resolved is False
    discussion.save.assert_not_called()


def test_insertion_at_flagged_position_does_not_resolve() -> None:
    # A line inserted where the comment used to point must not resolve the
    # thread: coordinates belong to the comment's head, not the current one.
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": INSERT_ONLY_PATCH}])
    provider.resolve_fixed_inline_threads()
    assert discussion.resolved is False


def test_thread_on_current_head_is_not_compared() -> None:
    discussion = _discussion(line=2, head_sha=HEAD_SHA)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.resolve_fixed_inline_threads()
    provider.gl.projects.get.return_value.repository_compare.assert_not_called()
    assert discussion.resolved is False


def test_disabled_flag_is_noop() -> None:
    get_settings().set("gitlab.auto_resolve_fixed_inline_threads", False)
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.resolve_fixed_inline_threads()
    provider.mr.discussions.list.assert_not_called()
    assert discussion.resolved is False


def test_non_agent_and_other_author_threads_stay_open() -> None:
    foreign = _discussion(line=2, body="a human comment")
    other_author = _discussion(line=2, author_id=999)
    provider = _provider([foreign, other_author], [{"old_path": "app.py", "diff": PATCH}])
    provider.resolve_fixed_inline_threads()
    assert foreign.resolved is False
    assert other_author.resolved is False


def test_is_fixed_own_inline_thread_guards() -> None:
    assert not _is_fixed_own_inline_thread(
        _discussion(resolved=True), BOT_ID, {"app.py": {2}})
    assert not _is_fixed_own_inline_thread(
        _discussion(body="human"), BOT_ID, {"app.py": {2}})
    assert _is_fixed_own_inline_thread(
        _discussion(line=2), BOT_ID, {"app.py": {2}})
