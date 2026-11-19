"""Bounded, layout-independent extraction for the investor import pipeline."""
import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_PAGES = 200
MAX_ROWS = 10000
MAX_CHARACTERS = 2_000_000


def extract_document(content, filename):
    ext = Path(filename).suffix.lower()
    pages = []

    def add(text, method, locator, review=False):
        if len(pages) >= MAX_PAGES or sum(p['characters'] for p in pages) + len(text) > MAX_CHARACTERS:
            raise ValueError('Документ превышает лимит страниц или извлечённого текста')
        pages.append({'page':len(pages)+1, 'raw_text':text, 'characters':len(text),
                      'text_method':method, 'source_locator':locator,
                      'needs_review':review or not bool(text.strip()), 'ocr_error':None})

    def table_rows(rows, sheet):
        chunk = []
        header = None
        for index, row in enumerate(rows, 1):
            if index > MAX_ROWS or len(row) > 200:
                raise ValueError('Таблица превышает лимит 10000 строк или 200 столбцов')
            values = [str(value) if value is not None else '' for value in row]
            if not any(values):
                continue
            if header is None:
                header = values
            chunk.append(f'row={index}: ' + ' | '.join(values))
            if len(chunk) >= 50:
                add('\n'.join(chunk), 'spreadsheet', f'sheet={sheet};through_row={index}', True)
                chunk = ['Headers: ' + ' | '.join(header)]
        if chunk:
            add('\n'.join(chunk), 'spreadsheet', f'sheet={sheet}', True)

    if ext == '.xlsx':
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            for sheet in workbook:
                table_rows(sheet.iter_rows(values_only=True), sheet.title)
        finally:
            workbook.close()
    elif ext == '.xls':
        import xlrd
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
        try:
            for sheet in workbook.sheets():
                table_rows((sheet.row_values(i) for i in range(sheet.nrows)), sheet.name)
        finally:
            workbook.release_resources()
    elif ext == '.csv':
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = content.decode('cp1251')
        if '\x00' in text:
            raise ValueError('CSV содержит бинарные данные')
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        table_rows(csv.reader(io.StringIO(text), dialect), 'CSV')
    elif ext in {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}:
        from PIL import Image
        import pytesseract
        import os
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                for index in range(getattr(image, 'n_frames', 1)):
                    image.seek(index)
                    if image.width * image.height > 40_000_000:
                        raise ValueError('Слишком большое изображение')
                    text = pytesseract.image_to_string(image.convert('RGB'), lang='rus+eng',
                        config='--oem 1 --psm 6', timeout=int(os.environ.get('TESSERACT_TIMEOUT', '90')))
                    add(text.strip(), 'tesseract_image', f'frame={index+1}', True)
    elif ext == '.pptx':
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            slides = sorted((n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                            key=lambda n: int(re.search(r'(\d+)\.xml', n)[1]))
            for name in slides:
                root = ET.fromstring(archive.read(name))
                text = '\n'.join(n.text or '' for n in root.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}t'))
                add(text, 'pptx_text', name, True)
    else:
        raise ValueError('Формат не поддерживается универсальным обработчиком')
    if not pages or not any(p['raw_text'].strip() for p in pages):
        raise ValueError('В документе не найден текст; проверьте источник')
    return {'ok': True, 'filename':filename, 'pages_count':len(pages),
            'total_characters':sum(p['characters'] for p in pages), 'pages':pages,
            'extraction_settings':{'manual_review_required':True, 'logical_pages':True}}
