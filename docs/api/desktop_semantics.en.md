# Targeted background UIA actions

`desktop_snapshot` keeps the active window as its default. On Windows, pass
the `{hwnd, pid, create_time}` identity returned by `desktop_list_windows` as
`target_window` to inspect another window. Snapshot refs retain that identity;
each fresh read validates the HWND, owning PID, and process creation time so a
recycled handle or restarted process is rejected.

For an explicitly targeted window, `desktop_act` supports only `set_value`
through a writable UI Automation `ValuePattern`. It never focuses the window,
uses `click_input`, or falls back to pixel input. The result includes the
read-back value and whether foreground HWND and cursor position stayed the
same. A control without writable ValuePattern is reported as not delivered.

The Windows fixture uses two synthetic off-screen windows shown with
`WS_EX_NOACTIVATE` and `SW_SHOWNOACTIVATE`; it changes one standard Edit
control and verifies the other remains unchanged. This demonstrates one
provider/control combination, not universal UIA or application compatibility.
Install the optional backend in Faustus's own environment with
`python -m pip install pywinauto`. The Python indicator handshake test uses a
synthetic acknowledgement in a temporary runtime directory; the existing
Electron test separately verifies that production overlays are
`focusable: false` and shown with `showInactive()`. It does not launch the
production Electron overlay during the physical UIA test.
See [the detailed Spanish API notes](desktop_semantics.md).
