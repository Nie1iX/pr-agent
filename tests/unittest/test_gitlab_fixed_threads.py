"""Unit tests for gitlab.auto_resolve_fixed_inline_threads."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.gitlab_provider import (
    GitLabProvider,
    _eligible_own_inline_thread,
    _flagged_line_removed,
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

NO_NEWLINE_PATCH = """\
@@ -1,3 +1,3 @@
 ctx
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""

DOUBLE_DASH_PATCH = """\
@@ -1,3 +1,3 @@
 ctx
---flag
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


def test_no_newline_marker_is_not_a_file_line() -> None:
    # "\ No newline at end of file" must not advance the base line counter.
    assert _removed_lines_from_patch(NO_NEWLINE_PATCH) == {2}


def test_double_dash_content_line_is_a_removal() -> None:
    # A removed source line "--flag" is encoded as "---flag" inside the hunk.
    assert _removed_lines_from_patch(DOUBLE_DASH_PATCH) == {2}


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


def test_ineligible_threads_skip_compare() -> None:
    # Human and already-resolved threads must not cost a repository_compare call.
    foreign = _discussion(line=2, body="a human comment")
    other_author = _discussion(line=2, author_id=999)
    resolved_thread = _discussion(line=2, resolved=True)
    provider = _provider([foreign, other_author, resolved_thread],
                         [{"old_path": "app.py", "diff": PATCH}])
    provider.resolve_fixed_inline_threads()
    provider.gl.projects.get.return_value.repository_compare.assert_not_called()
    for d in (foreign, other_author, resolved_thread):
        d.save.assert_not_called()


def test_object_shaped_compare_response_is_handled() -> None:
    discussion = _discussion(line=2)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.gl.projects.get.return_value.repository_compare.return_value = SimpleNamespace(
        diffs=[SimpleNamespace(new_path="app.py", old_path="app.py", diff=PATCH)])
    provider.resolve_fixed_inline_threads()
    assert discussion.resolved is True


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


def test_thread_eligibility_guards() -> None:
    assert _eligible_own_inline_thread(_discussion(resolved=True), BOT_ID) is None
    assert _eligible_own_inline_thread(_discussion(body="human"), BOT_ID) is None
    position = _eligible_own_inline_thread(_discussion(line=2), BOT_ID)
    assert position is not None
    assert _flagged_line_removed(position, {"app.py": {2}})
    assert not _flagged_line_removed(position, {"app.py": {3}})


def test_deletion_anchored_thread_stays_open() -> None:
    # old_line is a coordinate in the MR base, not the comment head: a head-side
    # removal at the same number must not resolve a deletion-anchored thread.
    position = {"position_type": "text", "new_path": "app.py", "old_path": "app.py",
                "old_line": 2, "head_sha": OLD_SHA}
    assert not _flagged_line_removed(position, {"app.py": {2}})


async def test_clean_rerun_still_resolves_fixed_threads(monkeypatch) -> None:
    import pr_agent.agent.pr_agent as pr_agent_module

    class FakeTool:
        def __init__(self, pr_url, ai_handler, args):
            pass

        async def run(self):
            pass

    provider = SimpleNamespace(resolve_fixed_inline_threads=MagicMock())
    provider_factory = MagicMock(return_value=provider)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda args: (True, None))
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", lambda args: args)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeTool)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", "/custom")
    assert handled is True
    provider_factory.assert_called_once_with("https://example/pr/1")
    provider.resolve_fixed_inline_threads.assert_called_once()


def test_repeat_sweep_costs_no_api_calls() -> None:
    # The post-command cleanup and the pre-publish cleanup share one provider;
    # the second call must not list discussions again.
    discussion = _discussion(line=3)
    provider = _provider([discussion], [{"old_path": "app.py", "diff": PATCH}])
    provider.resolve_fixed_inline_threads()
    provider.resolve_fixed_inline_threads()
    provider.mr.discussions.list.assert_called_once()


async def test_disabled_flag_skips_cleanup_dispatch(monkeypatch) -> None:
    import pr_agent.agent.pr_agent as pr_agent_module
    get_settings().set("gitlab.auto_resolve_fixed_inline_threads", False)

    class FakeTool:
        def __init__(self, pr_url, ai_handler, args):
            pass

        async def run(self):
            pass

    provider_factory = MagicMock()
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    monkeypatch.setattr(pr_agent_module.CliArgs, "validate_user_args", lambda args: (True, None))
    monkeypatch.setattr(pr_agent_module, "update_settings_from_args", lambda args: args)
    monkeypatch.setattr(pr_agent_module, "get_git_provider_with_context", provider_factory)
    monkeypatch.setitem(pr_agent_module.command2class, "custom", FakeTool)

    handled = await pr_agent_module.PRAgent(ai_handler="fake-ai")._handle_request(
        "https://example/pr/1", "/custom")
    assert handled is True
    provider_factory.assert_not_called()
