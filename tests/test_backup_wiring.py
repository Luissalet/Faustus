"""The backup service is actually reachable and actually scheduled (FAUSTUS).

A backup feature nobody triggers is the same as no backup feature, so the three
links get their own test: the app starts the loop, the API exposes list/take/
verify behind admin, and the UI has a command that calls them.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
ROUTES = (ROOT / "routes" / "backup_routes.py").read_text(encoding="utf-8")
COMMANDS = (ROOT / "studio" / "src" / "screens" / "studio" / "commands.ts").read_text(encoding="utf-8")
ADAPTER = (ROOT / "studio" / "src" / "adapters" / "commands.ts").read_text(encoding="utf-8")


def test_startup_launches_the_scheduled_snapshots():
    assert "from src.backup_service import run_auto_backups" in APP
    assert "asyncio.create_task(run_auto_backups())" in APP


def test_a_failed_backup_loop_cannot_stop_the_app_from_starting():
    idx = APP.index("from src.backup_service import run_auto_backups")
    assert "except Exception" in APP[idx:idx + 400]


def test_endpoints_exist_and_are_admin_only():
    for route in ("/api/backup/snapshots", "/api/backup/snapshot", "/api/backup/verify"):
        assert f'"{route}"' in ROUTES
    # One require_admin per endpoint, plus the two that were already there.
    assert ROUTES.count("require_admin(request)") >= 5


def test_verify_endpoint_confines_the_path_to_the_backup_dir():
    """Otherwise it is an admin-only 'read any tarball on disk' endpoint."""
    idx = ROUTES.index("/api/backup/verify")
    assert "resolve_in_backup_dir" in ROUTES[idx:idx + 900]


def test_there_is_no_restore_endpoint():
    """Restore overwrites data/ under a running app — it stays manual on purpose."""
    assert "/api/backup/restore" not in ROUTES


def test_slash_command_is_registered_and_calls_the_api():
    assert "name: 'backup'" in COMMANDS, "/backup is not in the command registry"
    for route in ("/api/backup/snapshots", "/api/backup/snapshot", "/api/backup/verify"):
        assert route in ADAPTER, f"{route} is never called"


def test_slash_command_does_not_squat_an_existing_alias():
    """`snapshots` already belongs to /checkpoints, and an alias that shadows
    another command silently steals it."""
    idx = COMMANDS.index("name: 'backup'")
    block = COMMANDS[idx:idx + 400]
    assert "'snapshots'" not in block
