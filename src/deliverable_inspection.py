"""Bounded, read-only structural inspection of workspace deliverables.

This reports what parsers can measure or extract. It does not render, score,
or certify visual/semantic quality. Package relationships are read as data;
external targets and embedded code are never executed or fetched.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import zipfile
import xml.etree.ElementTree as ET

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_NATIVE_DOCUMENT_BYTES = 32 * 1024 * 1024
DEFAULT_CONTENT_CHARS = 24_000
MAX_CONTENT_CHARS = 80_000
MAX_ROWS = 20_000
MAX_PDF_PAGES = 5_000
MAX_EXTRACTED_CELLS = 100_000
MAX_NATIVE_ITEMS = 100_000
NS = {
    'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'c': 'http://schemas.openxmlformats.org/drawingml/2006/chart',
    'rel': 'http://schemas.openxmlformats.org/package/2006/relationships',
    'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
}


class DeliverableInspectionError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _xml(package: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        info = package.getinfo(name)
    except KeyError as exc:
        raise DeliverableInspectionError(f"Package is missing required part '{name}'.", 'missing_part') from exc
    if info.file_size > MAX_XML_BYTES:
        raise DeliverableInspectionError(f"Package part '{name}' exceeds the XML inspection limit.", 'part_too_large')
    try:
        return ET.fromstring(package.read(name))
    except (ET.ParseError, KeyError, zipfile.BadZipFile) as exc:
        raise DeliverableInspectionError(f"Package part '{name}' is malformed.", 'invalid_package') from exc


def _rels(package: zipfile.ZipFile, source: str) -> dict[str, str]:
    folder, filename = source.rsplit('/', 1) if '/' in source else ('', source)
    relpath = f"{folder}/_rels/{filename}.rels" if folder else f"_rels/{filename}.rels"
    try:
        root = _xml(package, relpath)
    except DeliverableInspectionError as exc:
        if exc.code == 'missing_part':
            return {}
        raise
    result = {}
    for rel in root.findall('rel:Relationship', NS):
        if rel.get('TargetMode') == 'External':
            continue
        target = rel.get('Target', '')
        full = os.path.normpath(os.path.join(folder, target)).replace('\\', '/')
        if full.startswith('../') or full == '..':
            continue
        result[rel.get('Id', '')] = full.lstrip('/')
    return result


def _shape_text(shape: ET.Element) -> str:
    out = []
    for para in shape.findall('.//a:p', NS):
        text = ''.join(node.text or '' for node in para.findall('.//a:t', NS))
        if text.strip():
            out.append(text)
    return '\n'.join(out)


def _chart(package: zipfile.ZipFile, path: str) -> dict:
    root = _xml(package, path)
    title = ' '.join(t.text or '' for t in root.findall('.//c:title//a:t', NS)).strip()
    series = []
    for item in root.findall('.//c:ser', NS):
        name = ' '.join((x.text or '') for x in item.findall('./c:tx//c:v', NS)).strip()
        values = [x.text or '' for x in item.findall('./c:val//c:pt/c:v', NS)]
        cats = [x.text or '' for x in item.findall('./c:cat//c:pt/c:v', NS)]
        series.append({'name': name or None, 'categories': cats, 'values': values})
    return {'path': path, 'title': title or None, 'series': series}


def _pptx(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        _check_zip(infos)
        pres = _xml(z, 'ppt/presentation.xml')
        rels = _rels(z, 'ppt/presentation.xml')
        slides = []
        for index, item in enumerate(pres.findall('.//p:sldId', NS), 1):
            if index > MAX_ROWS:
                raise DeliverableInspectionError(f'Presentation has more than {MAX_ROWS} slides.', 'item_limit')
            part = rels.get(item.get(f"{{{NS['r']}}}id", ''), '')
            if not part.startswith('ppt/slides/'):
                continue
            root = _xml(z, part)
            relmap = _rels(z, part)
            shapes = []
            for shape in root.findall('.//p:spTree/*', NS):
                text = _shape_text(shape)
                if text:
                    shapes.append(text)
            notes = []
            for target in relmap.values():
                if '/notesSlides/' in f'/{target}' and target.endswith('.xml'):
                    note_root = _xml(z, target)
                    notes = [t.text or '' for t in note_root.findall('.//a:t', NS) if (t.text or '').strip()]
            media = []
            charts = []
            for target in relmap.values():
                if target.startswith('ppt/media/'):
                    info = z.getinfo(target)
                    media.append({'path': target, 'size_bytes': info.file_size, 'format': Path(target).suffix.lower()})
                    if target.lower().endswith('.svg'):
                        try:
                            svg = _xml(z, target)
                            media[-1]['viewBox'] = svg.get('viewBox')
                            media[-1]['width'] = svg.get('width')
                            media[-1]['height'] = svg.get('height')
                        except DeliverableInspectionError:
                            media[-1]['metadata_error'] = 'SVG XML could not be parsed'
                elif target.startswith('ppt/charts/') and target.endswith('.xml'):
                    charts.append(_chart(z, target))
            slides.append({'index': index, 'part': part, 'texts': shapes,
                           'notes': notes, 'images': media, 'charts': charts})
        return {'slide_count': len(slides), 'slides': slides,
                'embedded_media_count': sum(len(s['images']) for s in slides),
                'chart_count': sum(len(s['charts']) for s in slides)}


def _docx(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist(); _check_zip(infos)
        root = _xml(z, 'word/document.xml')
        paragraphs = []
        body = root.find('.//w:body', NS)
        for para_index, para in enumerate((body.findall('w:p', NS) if body is not None else root.findall('.//w:p', NS)), 1):
            if para_index > MAX_ROWS:
                raise DeliverableInspectionError(f'Document has more than {MAX_ROWS} paragraphs.', 'item_limit')
            text = ''.join(t.text or '' for t in para.findall('.//w:t', NS))
            if text.strip():
                paragraphs.append(text)
        tables = []
        for table in root.findall('.//w:tbl', NS):
            tables.append([[ ''.join(t.text or '' for t in cell.findall('.//w:t', NS))
                              for cell in row.findall('./w:tc', NS)]
                            for row in table.findall('./w:tr', NS)])
        images = [{'path': i.filename, 'size_bytes': i.file_size} for i in infos
                  if i.filename.startswith('word/media/')]
        return {'paragraph_count': len(paragraphs), 'paragraphs': paragraphs,
                'table_count': len(tables), 'tables': tables, 'images': images,
                'image_count': len(images)}


def _xlsx(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist(); _check_zip(infos)
        wb = _xml(z, 'xl/workbook.xml')
        relmap = _rels(z, 'xl/workbook.xml')
        shared = []
        try:
            shared_root = _xml(z, 'xl/sharedStrings.xml')
            shared = [''.join(t.text or '' for t in si.findall('.//x:t', NS)) for si in shared_root.findall('x:si', NS)]
        except DeliverableInspectionError as exc:
            if exc.code != 'missing_part': raise
        sheets = []; total_cells = formulas = 0
        for sh in wb.findall('.//x:sheets/x:sheet', NS):
            part = relmap.get(sh.get(f"{{{NS['r']}}}id", ''), '')
            if not part.startswith('xl/worksheets/'): continue
            root = _xml(z, part); rows = []; sheet_cells = 0; sheet_formulas = 0
            for row in root.findall('.//x:sheetData/x:row', NS):
                if len(rows) >= MAX_ROWS:
                                        raise DeliverableInspectionError(f"Sheet {sh.get('name')} has more than {MAX_ROWS} rows.", 'item_limit')
                cells = []
                for cell in row.findall('x:c', NS):
                    if total_cells + sheet_cells >= MAX_EXTRACTED_CELLS:
                        raise DeliverableInspectionError(f'Workbook has more than {MAX_EXTRACTED_CELLS} inspected cells.', 'item_limit')
                    value = cell.find('x:v', NS); formula = cell.find('x:f', NS)
                    raw = value.text if value is not None else None
                    kind = cell.get('t')
                    if kind == 's' and raw is not None:
                        try: raw = shared[int(raw)]
                        except (ValueError, IndexError): raw = None
                    elif kind == 'inlineStr':
                        raw = ''.join(t.text or '' for t in cell.findall('.//x:t', NS))
                    cells.append({'ref': cell.get('r'), 'value': raw,
                                  'formula': formula.text if formula is not None else None,
                                  'type': kind})
                    sheet_cells += 1; sheet_formulas += formula is not None
                if cells: rows.append({'index': row.get('r'), 'cells': cells})
            total_cells += sheet_cells; formulas += sheet_formulas
            sheets.append({'name': sh.get('name'), 'part': part, 'rows': rows,
                           'row_count': len(rows), 'cell_count': sheet_cells,
                           'formula_count': sheet_formulas,
                           'formula_cells_without_cached_value': sum(
                               1 for row in rows for cell in row['cells']
                               if cell['formula'] is not None and cell['value'] is None)})
        return {'sheet_count': len(sheets), 'sheets': sheets, 'cell_count': total_cells,
                'formula_count': formulas, 'calculation': 'formulas are reported, never recalculated'}


def _check_zip(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_FILE_BYTES * 2:
        raise DeliverableInspectionError('Office package exceeds the bounded inspection limits.', 'package_too_large')
    if any(i.file_size > MAX_XML_BYTES and i.filename.lower().endswith(('.xml', '.rels')) for i in infos):
        raise DeliverableInspectionError('Office package contains an oversized XML part.', 'part_too_large')


def _native_json(data: bytes, label: str) -> dict:
    if len(data) > MAX_NATIVE_DOCUMENT_BYTES:
        raise DeliverableInspectionError(f'{label} exceeds the native document inspection limit.', 'part_too_large')
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DeliverableInspectionError(f'{label} is malformed JSON.', 'invalid_package') from exc
    if not isinstance(value, dict):
        raise DeliverableInspectionError(f'{label} must contain a JSON object.', 'invalid_package')
    return value


def _designcraft(path: Path) -> dict:
    """Extract bounded structural facts from DesignCraft's ZIP native package."""
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        _check_zip(infos)
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise DeliverableInspectionError('DesignCraft package contains duplicate part names.', 'invalid_package')
        try:
            mime_info = z.getinfo('mimetype')
            document_info = z.getinfo('document.json')
        except KeyError as exc:
            raise DeliverableInspectionError('DesignCraft package is missing a required part.', 'missing_part') from exc
        if mime_info.file_size > 64:
            raise DeliverableInspectionError('DesignCraft mimetype part is oversized.', 'part_too_large')
        mime = z.read('mimetype')
        if mime != b'application/vnd.designcraft+zip':
            raise DeliverableInspectionError('Package mimetype is not DesignCraft.', 'invalid_package')
        if document_info.file_size > MAX_NATIVE_DOCUMENT_BYTES:
            raise DeliverableInspectionError('DesignCraft document exceeds the native document inspection limit.', 'part_too_large')
        document = _native_json(z.read('document.json'), 'DesignCraft document.json')

        spreads = document.get('spreads', [])
        parents = document.get('parents', [])
        stories = document.get('stories', {})
        assets = document.get('assets', {})
        if not isinstance(spreads, list) or not isinstance(parents, list) or not isinstance(stories, dict) or not isinstance(assets, dict):
            raise DeliverableInspectionError('DesignCraft document has invalid spreads, stories, or assets fields.', 'invalid_package')
        if len(spreads) + len(parents) > MAX_NATIVE_ITEMS or len(stories) > MAX_NATIVE_ITEMS or len(assets) > MAX_NATIVE_ITEMS:
            raise DeliverableInspectionError(f'DesignCraft document exceeds {MAX_NATIVE_ITEMS} structural records.', 'item_limit')

        counts = {'text': 0, 'graphic': 0, 'group': 0, 'unassigned': 0, 'other': 0}
        item_total = 0
        stack = []
        for spread in [*spreads, *parents]:
            if not isinstance(spread, dict):
                continue
            items = spread.get('items', [])
            if isinstance(items, list):
                stack.extend((item, 0) for item in items)
        while stack:
            item, depth = stack.pop()
            item_total += 1
            if item_total > MAX_NATIVE_ITEMS:
                raise DeliverableInspectionError(f'DesignCraft document exceeds {MAX_NATIVE_ITEMS} items.', 'item_limit')
            if depth > 256:
                raise DeliverableInspectionError('DesignCraft item nesting exceeds the inspection limit.', 'item_limit')
            content = item.get('content') if isinstance(item, dict) else None
            kind = content.get('type') if isinstance(content, dict) else None
            counts[kind if kind in {'text', 'graphic', 'group', 'unassigned'} else 'other'] += 1
            if kind == 'group' and isinstance(content.get('items'), list):
                stack.extend((child, depth + 1) for child in content['items'])

        text = []
        for story in stories.values():
            if isinstance(story, dict) and isinstance(story.get('text'), str):
                text.append(story['text'])

        pages = sum(len(spread.get('pages', [])) for spread in spreads if isinstance(spread, dict) and isinstance(spread.get('pages', []), list))
        parent_pages = sum(len(spread.get('pages', [])) for spread in parents if isinstance(spread, dict) and isinstance(spread.get('pages', []), list))
        if pages + parent_pages > MAX_NATIVE_ITEMS:
            raise DeliverableInspectionError(f'DesignCraft document exceeds {MAX_NATIVE_ITEMS} pages.', 'item_limit')
        asset_parts = [info for info in infos if info.filename not in {'mimetype', 'meta.json', 'document.json'}]
        linked_assets = sum(isinstance(asset, dict) and bool(asset.get('link')) for asset in assets.values())
        return {
            'native_format': 'DesignCraft', 'title': document.get('title'),
            'spread_count': len(spreads), 'page_count': pages,
            'parent_spread_count': len(parents), 'parent_page_count': parent_pages,
            'item_count': item_total, 'text_frame_count': counts['text'],
            'image_frame_count': counts['graphic'], 'group_count': counts['group'],
            'unassigned_item_count': counts['unassigned'], 'other_item_count': counts['other'],
            'story_count': len(stories), 'stories': text,
            'asset_record_count': len(assets), 'linked_asset_record_count': linked_assets,
            'non_metadata_package_part_count': len(asset_parts),
            'non_metadata_package_bytes': sum(info.file_size for info in asset_parts),
            'asset_note': 'Non-metadata package parts are counted without loading their bytes and are not assumed to be assets or attributed to frames; linked file paths are not followed.'}


def _vectorcraft(path: Path) -> dict:
    if path.stat().st_size > MAX_NATIVE_DOCUMENT_BYTES:
        raise DeliverableInspectionError('VectorCraft document exceeds the native document inspection limit.', 'part_too_large')
    with path.open('rb') as stream:
        raw = stream.read(MAX_NATIVE_DOCUMENT_BYTES + 1)
    document = _native_json(raw, 'VectorCraft document')
    if document.get('format') != 'vectorcraft' or not isinstance(document.get('document'), dict):
        raise DeliverableInspectionError('JSON does not contain a recognized VectorCraft document.', 'invalid_package')
    source = document['document']
    artboards = source.get('artboards', [])
    layers = source.get('layers', [])
    images = source.get('images', {})
    if not isinstance(artboards, list) or not isinstance(layers, list):
        raise DeliverableInspectionError('VectorCraft document has invalid artboards or layers.', 'invalid_package')
    if not isinstance(images, (dict, list)):
        raise DeliverableInspectionError('VectorCraft document has invalid image records.', 'invalid_package')
    if len(artboards) > MAX_NATIVE_ITEMS or len(images) > MAX_NATIVE_ITEMS:
        raise DeliverableInspectionError(f'VectorCraft document exceeds {MAX_NATIVE_ITEMS} structural records.', 'item_limit')
    counts = {'layer': 0, 'group': 0, 'path': 0, 'text': 0, 'image': 0, 'compound': 0, 'other': 0}
    stack = list(layers)
    total = 0
    while stack:
        node = stack.pop()
        total += 1
        if total > MAX_NATIVE_ITEMS:
            raise DeliverableInspectionError(f'VectorCraft document exceeds {MAX_NATIVE_ITEMS} nodes.', 'item_limit')
        kind_obj = node.get('kind') if isinstance(node, dict) else None
        kind = kind_obj.get('type') if isinstance(kind_obj, dict) else None
        counts[kind if kind in counts else 'other'] += 1
        if isinstance(kind_obj, dict):
            children = kind_obj.get('children', kind_obj.get('content', []))
            if isinstance(children, list):
                stack.extend(children)
    return {
        'native_format': 'VectorCraft', 'title': source.get('title'),
        'artboard_count': len(artboards), 'layer_count': counts['layer'],
        'group_count': counts['group'], 'path_count': counts['path'],
        'text_object_count': counts['text'], 'image_object_count': counts['image'],
        'compound_path_count': counts['compound'], 'other_node_count': counts['other'],
        'node_count': total, 'image_record_count': len(images),
        'image_note': 'Image records are counted without decoding pixels or following linked files.'}


def _pdf(path: Path) -> dict:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise DeliverableInspectionError('PDF is encrypted; text inspection requires an unlocked copy.', 'encrypted_pdf')
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DeliverableInspectionError(f'PDF has more than {MAX_PDF_PAGES} pages; split it before inspection.', 'page_limit')
        pages = []
        for i, page in enumerate(reader.pages):
            box = page.mediabox
            try: text = page.extract_text() or ''
            except Exception as exc: text = ''; issue = type(exc).__name__
            else: issue = None
            pages.append({'index': i + 1, 'width_points': float(box.width), 'height_points': float(box.height),
                          'rotation_degrees': int(page.get('/Rotate', 0) or 0), 'text': text,
                          **({'text_error': issue} if issue else {})})
        return {'page_count': len(pages), 'pages': pages, 'form_fields': len(reader.get_fields() or {}),
                'metadata': {str(k): str(v)[:500] for k, v in (reader.metadata or {}).items()}}
    except DeliverableInspectionError: raise
    except Exception as exc:
        raise DeliverableInspectionError(f'PDF could not be parsed ({type(exc).__name__}).', 'invalid_pdf') from exc


def _csv(path: Path, delimiter: str | None = None) -> dict:
    try:
        with path.open('r', encoding='utf-8-sig', newline='') as stream:
            sample = stream.read(8192)
    except UnicodeDecodeError as exc:
        raise DeliverableInspectionError('CSV is not UTF-8/UTF-8-BOM; convert or specify a text encoding before inspection.', 'encoding_error') from exc
    if delimiter is None:
        try: delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
        except csv.Error: delimiter = '\t' if path.suffix.lower() == '.tsv' else ','
    rows = []
    old_field_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(1024 * 1024)
        with path.open('r', encoding='utf-8-sig', newline='') as stream:
            for row in csv.reader(stream, delimiter=delimiter):
                rows.append(row)
                if len(rows) >= MAX_ROWS: break
    except csv.Error as exc:
        raise DeliverableInspectionError(f'Delimited text could not be parsed: {exc}', 'invalid_csv') from exc
    except UnicodeDecodeError as exc:
        raise DeliverableInspectionError('CSV contains invalid UTF-8 text.', 'encoding_error') from exc
    finally:
        csv.field_size_limit(old_field_limit)
    return {'delimiter': '\\t' if delimiter == '\t' else delimiter, 'row_count_inspected': len(rows),
            'column_count_max': max((len(r) for r in rows), default=0), 'rows': rows,
            'header': rows[0] if rows else [], 'row_limit_reached': len(rows) >= MAX_ROWS}


def _image_facts(path: Path) -> dict:
    try:
        from PIL import Image
        with Image.open(path) as image:
            return {'format': image.format, 'width': image.width, 'height': image.height,
                    'mode': image.mode, 'frame_count': getattr(image, 'n_frames', 1),
                    'metadata_keys': sorted(str(k) for k in image.info.keys())[:80]}
    except ImportError:
        return {'metadata_error': 'Pillow is unavailable; only file identity was measured.'}
    except Exception as exc:
        raise DeliverableInspectionError(f'Image could not be decoded ({type(exc).__name__}).', 'invalid_image') from exc


def _content_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def _bounded(value, budget: int):
    """Trim extractable values while retaining a useful, valid JSON object."""
    if isinstance(value, str):
        return value[:max(0, budget)]
    if isinstance(value, list):
        output = []; used = 2
        for item in value:
            if used >= budget: break
            bounded = _bounded(item, max(0, min(budget - used, 4000)))
            output.append(bounded); used += _content_size(bounded) + 1
        return output
    if isinstance(value, dict):
        output = {}; used = 2
        for key, item in value.items():
            if used >= budget: break
            bounded = _bounded(item, max(0, min(budget - used - len(key) - 4, 4000)))
            output[key] = bounded; used += len(key) + _content_size(bounded) + 4
        return output
    return value


async def inspect_deliverable(path_value: str, *, max_content_chars: int = DEFAULT_CONTENT_CHARS) -> dict:
    from src.tool_execution import _resolve_tool_path
    try: resolved = Path(_resolve_tool_path(path_value))
    except (TypeError, ValueError) as exc:
        raise DeliverableInspectionError(str(exc), 'path_not_allowed') from exc
    try:
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise DeliverableInspectionError('Path must refer to a regular file.', 'not_a_file')
        if info.st_size > MAX_FILE_BYTES:
            raise DeliverableInspectionError(f'File is {info.st_size} bytes; inspection limit is {MAX_FILE_BYTES}.', 'file_too_large')
        if not isinstance(max_content_chars, int) or isinstance(max_content_chars, bool) or not 1000 <= max_content_chars <= MAX_CONTENT_CHARS:
            raise DeliverableInspectionError(f'max_content_chars must be from 1000 to {MAX_CONTENT_CHARS}.', 'invalid_arguments')
        ext = resolved.suffix.lower()
        digest = hashlib.sha256()
        with resolved.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
        kind = 'other'; facts = {}; content = {}
        if ext == '.pdf': kind = 'pdf'; facts = _pdf(resolved)
        elif ext == '.pptx': kind = 'presentation'; facts = _pptx(resolved)
        elif ext == '.docx': kind = 'document'; facts = _docx(resolved)
        elif ext == '.xlsx': kind = 'spreadsheet'; facts = _xlsx(resolved)
        elif ext == '.designcraft': kind = 'native_design_project'; facts = _designcraft(resolved)
        elif ext == '.vectorcraft': kind = 'native_vector_project'; facts = _vectorcraft(resolved)
        elif ext in {'.csv', '.tsv'}: kind = 'delimited_text'; facts = _csv(resolved, '\t' if ext == '.tsv' else None)
        elif ext in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.svg'}:
            kind = 'image'
            if ext == '.svg':
                if info.st_size > MAX_XML_BYTES:
                    raise DeliverableInspectionError('SVG exceeds the XML inspection limit.', 'part_too_large')
                root = ET.parse(resolved).getroot()
                facts = {'format': 'SVG', 'width': root.get('width'), 'height': root.get('height'), 'viewBox': root.get('viewBox')}
            else: facts = _image_facts(resolved)
        elif ext in {'.mp4', '.mov', '.m4v', '.m4a', '.webm', '.mkv', '.mp3', '.flac', '.ogg', '.opus', '.aac', '.wav'}:
            kind = 'media'
            from src.media_inspection import inspect_media, MediaInspectionError
            try: facts = await inspect_media(str(resolved))
            except MediaInspectionError as exc:
                raise DeliverableInspectionError(str(exc), exc.code) from exc
        else:
            facts = {'metadata': 'No format parser is available; only file identity and hash were measured.'}
        if kind not in {'image', 'media', 'other'}: content = facts
        limited = _content_size(content) > max_content_chars
        if limited:
            # Both the structural payload and extractable text stay bounded.
            facts = _bounded(facts, max_content_chars)
            content = facts
        current = resolved.stat()
        if (info.st_size, info.st_mtime_ns, info.st_ino) != (current.st_size, current.st_mtime_ns, current.st_ino):
            raise DeliverableInspectionError('File changed during inspection; retry against a stable copy.', 'file_changed')
        return {'path': str(resolved), 'filename': resolved.name, 'extension': ext,
                'kind': kind, 'size_bytes': info.st_size, 'sha256': digest.hexdigest(),
                'status': 'inspected', 'facts': facts, 'content': content,
                'content_truncated': limited,
                'inspection_scope': 'Structural and extractable metadata only. No visual, audio, semantic, or overall quality rating is made.',
                'limitations': _limitations(kind, facts)}
    except DeliverableInspectionError: raise
    except OSError as exc:
        raise DeliverableInspectionError(f'File could not be read: {exc.strerror or type(exc).__name__}.', 'io_error') from exc
    except (ET.ParseError, zipfile.BadZipFile) as exc:
        raise DeliverableInspectionError('File structure is malformed or unsupported.', 'invalid_package') from exc


def _limitations(kind: str, facts: dict) -> list[str]:
    values = ['Inspection does not establish visual layout quality, completeness, correctness, or suitability for the task.']
    if kind in {'pdf', 'presentation', 'document', 'spreadsheet'}:
        values.append('Only text and structural metadata exposed by the parser are included; rendering may reveal other issues.')
    if kind == 'spreadsheet': values.append('Formula text and cached values are reported; formulas are not recalculated.')
    if kind == 'media': values.append('Media is not transcribed, listened to, or visually reviewed; metadata depends on available decoders.')
    if kind == 'image': values.append('Image pixels are not visually interpreted by this inspection.')
    if kind in {'native_design_project', 'native_vector_project'}:
        values.append('Native document structure and stored text are reported; the project is not rendered, and frame/node counts do not prove visibility, editability, completeness, or fidelity.')
    if kind == 'native_design_project': values.append('Embedded package parts are counted but not decoded or linked to frames; external linked file paths are never read.')
    if kind == 'native_vector_project': values.append('Image payloads and vector geometry are not rasterized or visually inspected; image records do not prove the referenced pixels are available.')
    return values
