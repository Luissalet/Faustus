"""Actual read-only policies must permit reads under live alias revocation."""
import pytest
import src.agent_tools  # noqa: F401
from src.autonomy_budget import is_read_only_tool
from src.step_snapshot import authorize_call, capture_step
from src.tool_authority import AUTHORITY
from src.tool_policy import ToolPolicy
from src.tool_security import email_tool_policy_names


def snapshot(name):
    return capture_step([AUTHORITY.emit(name)],session_id='qa',run_id='run',round_num=1,
                        candidate_index=0,tool_policy=ToolPolicy(read_only=True))


@pytest.mark.parametrize('name',['read_file','inspect_deliverable','get_workspace','glob','grep','ls'])
def test_announced_reads_are_allowed_with_actual_read_only_policy(name):
    policy=ToolPolicy(read_only=True)
    for spelling in email_tool_policy_names(name):
        assert is_read_only_tool(spelling),spelling
        verdict=authorize_call(snapshot(name),spelling,session_id='qa',tool_policy=policy)
        assert verdict.allowed,(spelling,verdict)


@pytest.mark.parametrize('name',['write_file','edit_file','bash','python'])
def test_mutator_aliases_remain_denied(name):
    for spelling in email_tool_policy_names(name):
        assert not is_read_only_tool(spelling),spelling
        verdict=authorize_call(snapshot(name),spelling,session_id='qa',tool_policy=ToolPolicy(read_only=True))
        assert not verdict.allowed,(spelling,verdict)


def test_explicit_revocation_under_alias_still_denies_read():
    snap=snapshot('read_file')
    verdict=authorize_call(snap,'read_file',session_id='qa',disabled_tools={'cat'},
                           tool_policy=ToolPolicy(read_only=True))
    assert not verdict.allowed and verdict.status=='revoked'


def test_unregistered_name_remains_denied():
    assert not is_read_only_tool('unknown_qa_read')
    verdict=authorize_call(snapshot('read_file'),'unknown_qa_read',session_id='qa',
                           tool_policy=ToolPolicy(read_only=True))
    assert not verdict.allowed
