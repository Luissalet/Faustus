"""Desktop observations after an action are not a loop."""
from src.agent_loop import _loop_signature


def test_screenshot_after_an_action_is_a_new_call():
    a = _loop_signature("desktop_screenshot", "{}", 0)
    b = _loop_signature("desktop_screenshot", "{}", 1)
    assert a != b
    assert _loop_signature("desktop_screenshot", "{}", 1) == b


def test_other_tools_keep_their_plain_signature():
    assert _loop_signature("bash", "ls", 0) == _loop_signature("bash", "ls", 5)