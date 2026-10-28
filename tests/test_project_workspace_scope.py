"""Regression tests for project task folders and sealed approval continuations."""
import ast
import copy
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workspace_selection_under_test", ROOT / "src/workspace_selection.py")
selection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selection)


class WorkspaceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = str(ROOT / "synthetic-project")
        self.child = str(Path(self.root) / "task" / "nested")

    def test_project_defaults_and_exact_root(self):
        for requested in ("", self.root):
            with self.subTest(requested=requested):
                self.assertEqual(selection.select_project_workspace(requested, self.root), (self.root, ""))

    def test_vetted_descendant_stays_the_execution_folder(self):
        self.assertEqual(selection.select_project_workspace(self.child, self.root), (self.child, ""))

    def test_nonproject_preserves_legacy_selection_and_rejection(self):
        self.assertEqual(selection.select_project_workspace(self.child, ""), (self.child, ""))
        self.assertEqual(selection.select_project_workspace("", "", "bad-path"), ("", "bad-path"))

    def test_existing_rejection_never_falls_back_to_project(self):
        self.assertEqual(selection.select_project_workspace("", self.root, "bad-path"), ("", "bad-path"))

    def test_sibling_prefix_and_parent_escape_are_rejected(self):
        for requested in (self.root + "-other", str(Path(self.root) / ".." / "elsewhere")):
            with self.subTest(requested=requested):
                self.assertEqual(selection.select_project_workspace(requested, self.root), ("", requested))

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_windows_case_and_normalization(self):
        requested = self.child.swapcase().replace("\\", "/")
        self.assertEqual(selection.select_project_workspace(requested, self.root), (requested, ""))

    @unittest.skipUnless(os.name == "nt", "Windows drive semantics")
    def test_other_drive_is_rejected(self):
        drive = "Y:" if Path(self.root).drive.upper() != "Y:" else "Z:"
        requested = drive + "\\task"
        self.assertEqual(selection.select_project_workspace(requested, self.root), ("", requested))

    def test_resolved_escape_without_creating_links(self):
        requested = str(Path(self.root) / "alias")
        actual_realpath = os.path.realpath
        def resolve(path):
            return str(ROOT / "outside") if path == requested else actual_realpath(path)
        with patch.object(selection.os.path, "realpath", side_effect=resolve):
            self.assertEqual(selection.select_project_workspace(requested, self.root), ("", requested))

    def test_canonicalization_failure_is_not_a_root_fallback(self):
        for error in (OSError("unavailable"), ValueError("invalid")):
            with self.subTest(error=type(error).__name__), patch.object(selection.os.path, "realpath", side_effect=error):
                self.assertEqual(selection.select_project_workspace(self.child, self.root), ("", self.child))


class SealedWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "routes/chat_routes.py").read_text(encoding="utf8"))
        cls.helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_project_harness_options")
        cls.restore = None
        for parent in ast.walk(tree):
            for _, nodes in ast.iter_fields(parent):
                if not isinstance(nodes, list):
                    continue
                for i, node in enumerate(nodes):
                    if (isinstance(node, ast.Assign) and isinstance(node.value, ast.BoolOp)
                            and isinstance(node.value.values[0], ast.Attribute)
                            and isinstance(node.value.values[0].value, ast.Name)
                            and node.value.values[0].value.id == "pending_tool_approval"
                            and node.value.values[0].attr == "workspace"):
                        end = next(j for j in range(i + 1, len(nodes)) if isinstance(nodes[j], ast.If))
                        cls.restore = nodes[i:end]
        if cls.restore is None:
            raise AssertionError("Sealed workspace restoration missing")

    def test_sealed_workspace_controls_trust_without_losing_turn_options(self):
        root = str(ROOT / "synthetic-project")
        child = str(Path(root) / "task")
        security = types.ModuleType("src.tool_security")
        security.owner_is_admin_or_single_user = lambda owner: owner == "owner"
        projects = types.ModuleType("services.projects")
        projects.project_for_session = lambda *args: {"workspace": root}
        projects.agent_options = lambda project: {"project_id": "same-project", "trusted": True, "test_command": "fixture-test"}
        with patch.dict(sys.modules, {"src.tool_security": security, "services.projects": projects}):
            env = {"Dict": dict, "Any": object, "get_current_user": lambda request: "owner",
                   "logger": types.SimpleNamespace(debug=lambda *args: None)}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(self.helper)], type_ignores=[])), "actual-project-helper", "exec"), env)
            for posted, sealed in ((root, child), (child, root), (root, "")):
                with self.subTest(posted=posted, sealed=sealed):
                    opts = env["_project_harness_options"](object(), "same-session", posted)
                    opts["typed_freshness_no"] = True
                    before = {key: value for key, value in opts.items() if key != "trusted_workspace"}
                    env.update(request=object(), session="same-session", workspace=posted,
                               workspace_rejected="", _harness_options=opts,
                               pending_tool_approval=types.SimpleNamespace(workspace=sealed))
                    exec(compile(ast.fix_missing_locations(ast.Module(body=copy.deepcopy(self.restore), type_ignores=[])), "actual-sealed-restore", "exec"), env)
                    self.assertEqual(env["workspace"], sealed or None)
                    self.assertEqual(env["_harness_options"].get("trusted_workspace"), os.path.realpath(root) if sealed == root else None)
                    self.assertEqual({key: value for key, value in opts.items() if key != "trusted_workspace"}, before)
                    self.assertIsNone(env["workspace_rejected"])


if __name__ == "__main__":
    unittest.main()
