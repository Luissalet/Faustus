import hashlib
import io
import json
import shutil
import subprocess
import struct
import zipfile

import pytest

from src.deliverable_inspection import inspect_deliverable, DeliverableInspectionError


def _zip(path, parts):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items(): z.writestr(name, data)


def _designcraft_fixture(path, *, with_asset=False):
    doc = {
        'title': 'Caption-only proof',
        'spreads': [{'pages': [{'id': 1}], 'items': [
            {'id': 2, 'content': {'type': 'text', 'story': 7}},
            {'id': 3, 'content': {'type': 'group', 'items': [
                {'id': 4, 'content': {'type': 'graphic', 'asset': 8}},
            ]}},
        ]}],
        'parents': [],
        'stories': {'7': {'id': 7, 'text': 'Triangle shown in caption only', 'frames': [2]}},
        'assets': {'8': {'id': 8, 'name': 'triangle.svg', 'mime': 'image/svg+xml', 'link': None}},
    }
    parts = {'mimetype': b'application/vnd.designcraft+zip',
             'meta.json': b'{}', 'document.json': json.dumps(doc).encode()}
    if with_asset:
        parts['assets/8'] = b'<svg/>'
    _zip(path, parts)


@pytest.mark.asyncio
async def test_csv_returns_real_content_identity_and_no_quality_claim(tmp_path):
    path = tmp_path / 'sample.csv'
    path.write_text('name,value\nAlpha,42\nBeta,17\n', encoding='utf-8')
    original = path.read_bytes()
    result = await inspect_deliverable(str(path))
    assert result['sha256'] == hashlib.sha256(original).hexdigest()
    assert result['size_bytes'] == len(original)
    assert result['facts']['rows'] == [['name', 'value'], ['Alpha', '42'], ['Beta', '17']]
    assert 'quality' in result['inspection_scope']
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_content_budget_truncates_without_invalid_json(tmp_path):
    path = tmp_path / 'long.csv'
    path.write_text('text\n' + ('A' * 2400) + '\n', encoding='utf-8')
    result = await inspect_deliverable(str(path), max_content_chars=1000)
    assert result['content_truncated'] is True
    assert len(json.dumps(result['content'], ensure_ascii=False)) < 1200


@pytest.mark.asyncio
async def test_normal_handler_returns_structured_inspection_receipt(tmp_path):
    from src.agent_tools import TOOL_HANDLERS
    path = tmp_path / 'receipt.csv'
    path.write_text('item\nreal artifact\n', encoding='utf-8')
    result = await TOOL_HANDLERS['inspect_deliverable'](json.dumps({'path': str(path)}), {})
    assert result['exit_code'] == 0
    receipt = result['deliverable']
    assert receipt['filename'] == 'receipt.csv'
    assert receipt['facts']['rows'][1][0] == 'real artifact'
    assert json.loads(result['output'])['sha256'] == receipt['sha256']


@pytest.mark.asyncio
async def test_pptx_extracts_slide_text_notes_svg_and_chart_values(tmp_path):
    path = tmp_path / 'sample.pptx'
    _zip(path, {
        'ppt/presentation.xml': '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="1" r:id="rId1"/></p:sldIdLst></p:presentation>',
        'ppt/_rels/presentation.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="slides/slide1.xml" Type="slide"/></Relationships>',
        'ppt/slides/slide1.xml': '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Delivery</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
        'ppt/slides/_rels/slide1.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="n1" Target="../notesSlides/notesSlide1.xml" Type="notesSlide"/><Relationship Id="c1" Target="../charts/chart1.xml" Type="chart"/><Relationship Id="i1" Target="../media/icon.svg" Type="image"/></Relationships>',
        'ppt/notesSlides/notesSlide1.xml': '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Speaker note</a:t></p:notes>',
        'ppt/media/icon.svg': '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" viewBox="0 0 20 10"/>',
        'ppt/charts/chart1.xml': '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>Volume</a:t></a:r></a:p></c:rich></c:tx></c:title><c:plotArea><c:barChart><c:ser><c:tx><c:v>North</c:v></c:tx><c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>9</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>',
    })
    result = await inspect_deliverable(str(path))
    slide = result['facts']['slides'][0]
    assert result['facts']['slide_count'] == 1
    assert slide['texts'] == ['Delivery']
    assert 'Speaker note' in slide['notes']
    assert slide['images'][0]['viewBox'] == '0 0 20 10'
    assert slide['charts'][0]['series'][0]['values'] == ['9']


@pytest.mark.asyncio
async def test_docx_and_xlsx_extract_paragraph_tables_and_formula_without_recalc(tmp_path):
    doc = tmp_path / 'sample.docx'
    _zip(doc, {'word/document.xml': '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Paragraph</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'})
    facts = (await inspect_deliverable(str(doc)))['facts']
    assert facts['paragraphs'] == ['Paragraph'] and facts['tables'] == [[['Cell']]]
    book = tmp_path / 'sample.xlsx'
    _zip(book, {
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Revenue</t></is></c></row><row r="2"><c r="A2"><f>SUM(B2:B4)</f></c></row></sheetData></worksheet>'})
    facts = (await inspect_deliverable(str(book)))['facts']
    assert facts['sheet_count'] == 1 and facts['formula_count'] == 1
    assert facts['sheets'][0]['formula_cells_without_cached_value'] == 1
    assert 'never recalculated' in facts['calculation']


@pytest.mark.asyncio
async def test_pdf_real_page_and_truncation(tmp_path):
    pypdf = pytest.importorskip('pypdf')
    from reportlab.pdfgen import canvas
    path = tmp_path / 'sample.pdf'
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(612, 792)); pdf.drawString(72, 720, 'Extractable PDF delivery'); pdf.save()
    path.write_bytes(buffer.getvalue())
    facts = (await inspect_deliverable(str(path)))['facts']
    assert facts['page_count'] == 1
    assert facts['pages'][0]['width_points'] == 612
    assert 'Extractable PDF delivery' in facts['pages'][0]['text']


@pytest.mark.asyncio
async def test_image_and_pcm_wav_return_measured_metadata(tmp_path):
    Image = pytest.importorskip('PIL.Image')
    image_path = tmp_path / 'pixel.png'
    Image.new('RGBA', (7, 5), (10, 20, 30, 255)).save(image_path)
    image = await inspect_deliverable(str(image_path))
    assert image['facts']['width'] == 7 and image['facts']['height'] == 5
    wav_path = tmp_path / 'tone.wav'
    sample_rate = 8000
    pcm = struct.pack('<h', 0) * sample_rate
    wav_path.write_bytes(b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVEfmt ' +
                         struct.pack('<IHHIIHH', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16) +
                         b'data' + struct.pack('<I', len(pcm)) + pcm)
    audio = await inspect_deliverable(str(wav_path))
    assert audio['kind'] == 'media'
    assert audio['facts']['format'] == 'WAV'
    assert audio['facts']['duration_seconds'] == pytest.approx(1.0, abs=.01)


@pytest.mark.asyncio
async def test_video_uses_ffprobe_metadata_when_encoder_is_available(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg/FFprobe are optional system tools')
    path = tmp_path / 'generated.mp4'
    try:
        subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'color=c=red:s=32x24:d=1', '-t', '1', '-pix_fmt', 'yuv420p', str(path)],
                       check=True, timeout=20, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip('Installed FFmpeg does not provide a synthetic video encoder')
    result = await inspect_deliverable(str(path))
    streams = result['facts'].get('streams', [])
    video = next(stream for stream in streams if stream.get('type') == 'video')
    assert video.get('width') == 32 and video.get('height') == 24


@pytest.mark.asyncio
async def test_invalid_package_and_bad_content_limit_have_actionable_codes(tmp_path):
    bad = tmp_path / 'bad.pptx'; bad.write_text('not a package')
    with pytest.raises(DeliverableInspectionError) as err:
        await inspect_deliverable(str(bad))
    assert err.value.code == 'invalid_package'
    good = tmp_path / 'good.csv'; good.write_text('x,y\n1,2')
    with pytest.raises(DeliverableInspectionError) as err:
        await inspect_deliverable(str(good), max_content_chars=10)
    assert err.value.code == 'invalid_arguments'


@pytest.mark.asyncio
async def test_designcraft_counts_pages_frames_stories_and_package_parts_without_claiming_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr('src.tool_execution._resolve_tool_path', lambda value: value)
    path = tmp_path / 'handout.designcraft'
    _designcraft_fixture(path, with_asset=True)
    original = path.read_bytes()
    result = await inspect_deliverable(str(path))
    facts = result['facts']
    assert result['kind'] == 'native_design_project'
    assert result['sha256'] == hashlib.sha256(original).hexdigest()
    assert facts['page_count'] == 1
    assert facts['text_frame_count'] == 1
    assert facts['image_frame_count'] == 1
    assert facts['group_count'] == 1 and facts['item_count'] == 3
    assert facts['stories'] == ['Triangle shown in caption only']
    assert facts['asset_record_count'] == 1 and facts['non_metadata_package_part_count'] == 1
    assert any('not rendered' in value for value in result['limitations'])
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_designcraft_requires_native_mimetype(tmp_path, monkeypatch):
    monkeypatch.setattr('src.tool_execution._resolve_tool_path', lambda value: value)
    path = tmp_path / 'wrong.designcraft'
    _zip(path, {'mimetype': b'not-designcraft', 'document.json': b'{}'})
    with pytest.raises(DeliverableInspectionError) as err:
        await inspect_deliverable(str(path))
    assert err.value.code == 'invalid_package'


@pytest.mark.asyncio
async def test_designcraft_enforces_document_json_size_limit(tmp_path, monkeypatch):
    import src.deliverable_inspection as inspection
    monkeypatch.setattr('src.tool_execution._resolve_tool_path', lambda value: value)
    monkeypatch.setattr(inspection, 'MAX_NATIVE_DOCUMENT_BYTES', 32)
    path = tmp_path / 'large.designcraft'
    _zip(path, {'mimetype': b'application/vnd.designcraft+zip', 'document.json': b' ' * 33})
    with pytest.raises(DeliverableInspectionError) as err:
        await inspect_deliverable(str(path))
    assert err.value.code == 'part_too_large'


@pytest.mark.asyncio
async def test_vectorcraft_counts_nodes_artboards_and_images_without_decoding(tmp_path, monkeypatch):
    monkeypatch.setattr('src.tool_execution._resolve_tool_path', lambda value: value)
    path = tmp_path / 'drawing.vectorcraft'
    source = {
        'format': 'vectorcraft', 'version': 3,
        'document': {'title': 'Star', 'artboards': [{'id': 1}],
                     'layers': [{'kind': {'type': 'layer', 'children': [
                         {'kind': {'type': 'path'}},
                         {'kind': {'type': 'text'}},
                         {'kind': {'type': 'image'}},
                     ]}}], 'images': {'image-1': {'mime': 'image/png'}}},
    }
    path.write_text(json.dumps(source), encoding='utf-8')
    original = path.read_bytes()
    result = await inspect_deliverable(str(path))
    facts = result['facts']
    assert result['kind'] == 'native_vector_project'
    assert result['sha256'] == hashlib.sha256(original).hexdigest()
    assert facts['artboard_count'] == 1 and facts['node_count'] == 4
    assert facts['layer_count'] == 1 and facts['path_count'] == 1
    assert facts['text_object_count'] == 1 and facts['image_object_count'] == 1
    assert facts['image_record_count'] == 1
    assert any('not rasterized' in value for value in result['limitations'])


@pytest.mark.asyncio
async def test_vectorcraft_rejects_unknown_json_format(tmp_path, monkeypatch):
    monkeypatch.setattr('src.tool_execution._resolve_tool_path', lambda value: value)
    path = tmp_path / 'not-vectorcraft.vectorcraft'
    path.write_text('{"format":"other","document":{}}', encoding='utf-8')
    with pytest.raises(DeliverableInspectionError) as err:
        await inspect_deliverable(str(path))
    assert err.value.code == 'invalid_package'


def test_tool_is_discoverable_read_only_and_workspace_untrusted():
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS, TOOL_TAGS
    from src.tool_capabilities import capabilities_for_tool, ToolEffect, ResultIntegrity
    from src.tool_security import PLAN_MODE_READONLY_TOOLS
    schema = next(x['function'] for x in FUNCTION_TOOL_SCHEMAS if x['function']['name'] == 'inspect_deliverable')
    assert schema['parameters']['required'] == ['path']
    assert 'max_content_chars' in schema['parameters']['properties']
    assert {'inspect_deliverable'} <= TOOL_TAGS & PLAN_MODE_READONLY_TOOLS
    caps = capabilities_for_tool('inspect_deliverable')
    assert ToolEffect.READ_WORKSPACE in caps.effects
    assert caps.result_integrity == ResultIntegrity.WORKSPACE_UNTRUSTED
