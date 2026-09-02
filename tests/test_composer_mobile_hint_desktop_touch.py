"""The composer's mobile behaviour (alternating "Swipe to toggle plan"
placeholder, picker auto-hide while typing) is gated on a PHONE-sized
viewport, not on touch capability. Seen live (ronda 6): a Windows desktop
with a touch screen reports maxTouchPoints > 0 and showed the swipe hint
every 5 s on a 1920 px window."""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_mobile_composer_mode_requires_a_narrow_viewport():
    src = open(os.path.join(_ROOT, "static", "app.js"), encoding="utf-8").read()
    block = src[src.index("function initModelPickerResponsive()"):]
    block = block[:block.index("checkPickerOverflow();\n  })();")]
    m = re.search(r"const _isMobile = (.+);", block)
    assert m, "the mobile flag disappeared"
    expr = m.group(1)
    assert "maxTouchPoints" in expr                      # touch is still part of it…
    assert "_isMobileChatInput()" in expr                # …AND a phone-sized viewport
    # the width helper is the single source of truth for "mobile chat input"
    assert "function _isMobileChatInput() {\n  return window.innerWidth <= 768;\n}" in src
