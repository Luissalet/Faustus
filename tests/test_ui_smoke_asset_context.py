"""CPU-only asset-context checks with real functions and tiny RAM snippets."""
import ast
import inspect
import ntpath
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from urllib.parse import quote, unquote, urljoin, urlsplit

SOURCE=Path(__file__).resolve().parents[1]/'src/ui_smoke.py'
ROOT=r'X:\asset-context-metadata'

def functions():
    tree=ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    names={'_extract_asset_refs','_static_server_spec','_crawl','_check_asset'}
    nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names
        or isinstance(node,ast.Assign) and not any(isinstance(t,ast.Name) and t.id=='logger' for t in node.targets)]
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    path=SimpleNamespace(**{name:getattr(ntpath,name) for name in ('join','abspath','dirname','commonpath','relpath')},isfile=lambda _:False)
    scope=dict(re=re,quote=quote,unquote=unquote,urljoin=urljoin,urlsplit=urlsplit,
        os=SimpleNamespace(path=path,sep='\\'),time=SimpleNamespace(time=lambda:0))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[])),'[real asset-context functions only]','exec'),scope)
    return scope

class AssetContextTests(unittest.TestCase):
    def extract(self,text):return functions()['_extract_asset_refs'](text)

    def test_minimal_import_class_is_not_a_javascript_statement(self):
        self.assertEqual(self.extract('<section class="import"><label for="x">X</label></section>'),[])

    def test_real_html_assets_are_not_polluted_by_classes_and_labels(self):
        html='<script src="/static/widget.js"></script><link href="/static/widget.css" rel="stylesheet">'
        html+='<section class="import"><label for="x">X</label></section>'
        self.assertEqual(self.extract(html),['/static/widget.js','/static/widget.css'])

    def test_html_comments_text_id_and_attributes_do_not_create_js_assets(self):
        for text in ('<!-- import "./comment.js" -->',
                     '<p>import "./text.js"</p>',
                     '<section id="import"><label for="x">X</label></section>',
                     '<div title="import \'./attribute.js\'"></div>'):
            with self.subTest(text=text):self.assertEqual(self.extract(text),[])

    def test_plain_javascript_import_forms_remain_supported(self):
        js='import value from "./module.js"; import "./side.js"; import("./lazy.js");'
        self.assertEqual(self.extract(js),['./module.js','./side.js','./lazy.js'])

    def test_javascript_with_markup_string_preserves_actual_imports(self):
        js='const template = "<section>text</section>"; import "./side.js"; import("./lazy.js");'
        self.assertEqual(self.extract(js),['./side.js','./lazy.js'])

    def test_inline_classic_and_module_scripts_are_javascript_contexts(self):
        for opening in ('<script>','<script type="module">','<script type="text/javascript">'):
            html=opening+'import value from "./module.js"; import "./side.js"; import("./lazy.js");</script>'
            with self.subTest(opening=opening):self.assertEqual(self.extract(html),['./module.js','./side.js','./lazy.js'])

    def test_data_script_and_style_contents_are_not_javascript(self):
        for opening,closing in (('<script type="application/json">','</script>'),
                                ('<script type="application/ld+json">','</script>'),
                                ('<script type="text/plain">','</script>'),('<style>','</style>')):
            html=opening+'import "./not-code.js"'+closing
            with self.subTest(opening=opening):self.assertEqual(self.extract(html),[])

    def test_external_script_does_not_execute_its_inline_data(self):
        html='<script src="/static/widget.js">import "./ignored-inline.js";</script>'
        self.assertEqual(self.extract(html),['/static/widget.js'])

    def test_html_attribute_entities_are_browser_urls_not_js_candidates(self):
        html='<script src="/static/widget.js?a=1&amp;b=2"></script><link href="/static/widget.css?v=1&amp;x=2">'
        self.assertEqual(self.extract(html),['/static/widget.js?a=1&b=2','/static/widget.css?v=1&x=2'])

    def test_external_data_and_duplicate_filter_contract_is_retained(self):
        html='<script src="https://example.invalid/ext.js"></script><script src="//example.invalid/ext.js"></script>'
        html+='<script src="data:text/javascript,none"></script><script src="mailto:marker"></script>'
        html+='<script src="/static/widget.js"></script><script src="/static/widget.js"></script>'
        self.assertEqual(self.extract(html),['/static/widget.js'])
        js='import "./same.js"; import("./same.js"); import "https://example.invalid/ext.js";'
        self.assertEqual(self.extract(js),['./same.js'])

    def test_explicit_context_resolves_the_legacy_ambiguous_input(self):
        extract=functions()['_extract_asset_refs']
        self.assertIn('is_html',inspect.signature(extract).parameters)
        self.assertEqual(extract('Text before <p>import "./not-code.js"</p>',is_html=True),[])
        self.assertEqual(extract('import "./real.js";',is_html=False),['./real.js'])

    def test_known_html_callers_propagate_context_and_ignore_text_imports(self):
        scope=functions();calls=[];original=scope['_extract_asset_refs']
        def extraction(text,**kwargs):calls.append(kwargs);return original(text,**kwargs)
        scope['_extract_asset_refs']=extraction
        html='Text before <p>import "./not-code.js"</p>'
        scope['_read_text']=lambda _:html
        scope['_static_server_spec'](ROOT,ROOT)
        self.assertTrue(calls);self.assertTrue(all(row.get('is_html') is True for row in calls))
        calls.clear();scope['_fetch']=lambda _:(200,'text/html',html.encode())
        pages,assets,problems=scope['_crawl']({'source':''},'http://metadata.invalid',10)
        self.assertEqual(assets,[]);self.assertEqual(problems,[])
        self.assertTrue(calls);self.assertTrue(all(row.get('is_html') is True for row in calls))

    def test_real_asset_status_and_mime_errors_stay_errors(self):
        scope=functions()
        for reply,problem in (((404,'text/html',b''),'status'),((200,'text/plain',b''),'content_type')):
            with self.subTest(reply=reply):
                scope['_fetch']=lambda _:reply
                result=scope['_check_asset']('http://metadata.invalid/widget.js')
                self.assertFalse(result['ok']);self.assertEqual(result['problem'],problem)

if __name__=='__main__':unittest.main(verbosity=2)
