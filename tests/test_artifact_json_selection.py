"""JSON selection and legacy read behavior at the native tool boundary."""
import asyncio
import ast
import importlib.util
from pathlib import Path
import json
import sys
import types
import unittest
from unittest.mock import patch
_spec = importlib.util.spec_from_file_location(
    "artifact_reader_unit", Path(__file__).resolve().parents[1] / "src/agent_tools/artifact_read_tool.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
ReadArtifactTool, TOOL_SCHEMA = _module.ReadArtifactTool, _module.TOOL_SCHEMA


class JsonArtifactSelectionTests(unittest.TestCase):
    def read(self, document, args):
        self.read_calls = []
        access = types.ModuleType('src.tool_result_offload')
        access.ArtifactAccessDenied = LookupError
        def stored(artifact_id, **kwargs):
            self.read_calls.append((artifact_id, kwargs))
            return {'text': json.dumps(document), 'total_chars': 99}
        access.read_artifact_range = stored
        owner = types.ModuleType('src.owner_identity')
        owner.effective_storage_owner = lambda *a, **kw: 'reader'
        with patch.dict(sys.modules, {'src.tool_result_offload': access,
                                     'src.owner_identity': owner}):
            return asyncio.run(ReadArtifactTool().execute(
                {'artifact_id': 'occ_test', **args}, {'owner': 'reader'}))

    def test_native_schema_exposes_selection(self):
        props = TOOL_SCHEMA['function']['parameters']['properties']
        for name in ('json_source', 'json_pointer', 'match_pointer', 'match_value'):
            self.assertIn(name, props)

    def test_native_catalogue_matches_handler_schema(self):
        path = Path(__file__).resolve().parents[1] / 'src/tool_schemas.py'
        tree = ast.parse(path.read_text(encoding='utf8'))
        schemas = next(node.value for node in tree.body
                       if isinstance(node, ast.Assign) and any(
                           isinstance(target, ast.Name) and target.id == 'FUNCTION_TOOL_SCHEMAS'
                           for target in node.targets))
        for node in schemas.elts:
            try:
                schema = ast.literal_eval(node)
            except (ValueError, TypeError):
                continue
            if schema.get('function', {}).get('name') == 'read_artifact':
                self.assertEqual(schema, TOOL_SCHEMA)
                break
        else:
            self.fail('read_artifact absent from native catalogue')

    def test_structured_source_and_complete_rows(self):
        rows = [{'ref': {'id': 8}, 'slots': [0, 3, 0], 'rate': 4},
                {'ref': {'id': 9}}, {'ref': {'id': 8}, 'rate': 1}]
        result = self.read({'data': {'rows': rows}}, {
            'json_source': '/data', 'json_pointer': '/rows',
            'match_pointer': '/ref/id', 'match_value': 8})
        value = json.loads(result['output'])
        self.assertEqual(value['value'], [rows[0], rows[2]])
        self.assertEqual((value['matched_items'], value['total_items']), (2, 3))
        self.assertEqual(self.read_calls, [('occ_test', {'owner': 'reader'})])

    def test_embedded_body_is_decoded_once(self):
        result = self.read({'output': json.dumps({'text': 'hello'})}, {
            'json_source': '/output', 'json_pointer': '/text'})
        self.assertEqual(json.loads(result['output'])['value'], 'hello')
        double = self.read({'output': json.dumps(json.dumps({'text': 1}))}, {
            'json_source': '/output', 'json_pointer': '/text'})
        self.assertEqual(double['exit_code'], 1)

    def test_false_and_zero_remain_distinct(self):
        result = self.read([False, 0, True, 1, None], {
            'json_pointer': '', 'match_pointer': '', 'match_value': False})
        self.assertEqual(json.loads(result['output'])['value'], [False])

    def test_missing_is_not_null(self):
        result = self.read([{}, {'x': None}, {'x': False}], {
            'json_pointer': '', 'match_pointer': '/x', 'match_value': None})
        self.assertEqual(json.loads(result['output'])['value'], [{'x': None}])

    def test_invalid_array_indices_do_not_become_python_indices(self):
        for pointer in ('/-1', '/01', '/-', '/2'):
            with self.subTest(pointer=pointer):
                self.assertEqual(self.read([3, 4], {'json_pointer': pointer})['exit_code'], 1)

    def test_invalid_filter_shape_is_explicit(self):
        for args in ({'json_source': ''}, {'json_pointer': '', 'match_value': 4},
                     {'json_pointer': '', 'match_pointer': ''},
                     {'json_pointer': '', 'match_pointer': '', 'match_value': []},
                     {'json_pointer': '', 'match_pointer': '/x~2', 'match_value': 1}):
            with self.subTest(args=args):
                self.assertEqual(self.read([], args)['exit_code'], 1)

    def test_query_and_selection_are_distinct_modes(self):
        for key, value in (('query', 'needle'), ('start', 0), ('end', 20)):
            self.assertEqual(self.read([], {'json_pointer': '', key: value})['exit_code'], 1)

    def test_legacy_reader_result_is_not_wrapped(self):
        result = self.read({'x': 4}, {'query': 'x'})
        self.assertEqual(result, {'text': '{"x": 4}', 'total_chars': 99})
        self.assertEqual(self.read_calls, [('occ_test', {
            'owner': 'reader', 'start': 0, 'end': None, 'query': 'x'})])


if __name__ == '__main__':
    unittest.main()
