"""Artificial document fixtures; actual Tesseract tests skip if not installed."""
import importlib.util
import io
import os
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

OCR_DIR = Path(__file__).resolve().parents[1] / 'services/gab-ocr'
text_spec = importlib.util.spec_from_file_location('document_text', OCR_DIR / 'document_text.py')
text_module = importlib.util.module_from_spec(text_spec)
text_spec.loader.exec_module(text_module)
sys.modules['document_text'] = text_module
progress_spec=importlib.util.spec_from_file_location('progress', OCR_DIR/'progress.py')
progress_module=importlib.util.module_from_spec(progress_spec)
progress_spec.loader.exec_module(progress_module)
sys.modules['progress']=progress_module


class DocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['OCR_API_TOKEN'] = 'synthetic-token'
        spec = importlib.util.spec_from_file_location('gab_test_ocr', OCR_DIR / 'app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from fastapi.testclient import TestClient
        cls.client = TestClient(module.app)

    def extract(self, name, content, endpoint='/extract/document-text'):
        response = self.client.post(endpoint, files={'file':(name, content)}, headers={'X-API-Key':'synthetic-token'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_pdf_text_multiple_pages(self):
        import fitz
        doc = fitz.open()
        for i in range(2):
            page = doc.new_page()
            page.insert_text((40,60), f'Synthetic investor company page {i+1}. This is an artificial financial document with selectable text.')
        result = self.extract('synthetic.pdf', doc.tobytes())
        self.assertEqual(result['pages_count'], 2)
        self.assertIn('Synthetic investor', result['pages'][1]['raw_text'])
        doc.close()

    def test_docx(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf,'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic investor</w:t></w:r></w:p></w:body></w:document>')
        self.assertIn('Synthetic investor', self.extract('synthetic.docx', buf.getvalue())['pages'][0]['raw_text'])

    def test_xlsx_all_sheets(self):
        import openpyxl
        book = openpyxl.Workbook()
        book.active.title = 'First'
        book.active.append(['Company','Revenue'])
        book.active.append(['Synthetic First',150000000])
        book.create_sheet('Second').append(['Synthetic Second',200000000])
        buf=io.BytesIO(); book.save(buf); book.close()
        result=self.extract('synthetic.xlsx', buf.getvalue())
        self.assertEqual(result['pages_count'], 2)
        self.assertIn('Second', result['pages'][1]['source_locator'])
        self.assertIn('Synthetic Second', result['pages'][1]['raw_text'])

    def test_xls_all_sheets(self):
        import xlwt
        book=xlwt.Workbook()
        for name in ['First','Second']:
            book.add_sheet(name).write(0,0,'Synthetic '+name)
        buf=io.BytesIO(); book.save(buf)
        result=self.extract('synthetic.xls', buf.getvalue())
        self.assertEqual(result['pages_count'],2)
        self.assertIn('Synthetic Second', result['pages'][1]['raw_text'])

    def test_csv(self):
        result=self.extract('synthetic.csv', 'Компания;Выручка\nИскусственная компания;150000000'.encode('cp1251'))
        self.assertIn('Искусственная компания', result['pages'][0]['raw_text'])

    def test_pptx_text(self):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as archive:
            archive.writestr('ppt/slides/slide1.xml','<a:root xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Synthetic slide</a:t></a:root>')
        self.assertIn('Synthetic slide', self.extract('synthetic.pptx',buf.getvalue())['pages'][0]['raw_text'])

    def test_corrupt_and_missing_doc_runtime(self):
        for filename in ['bad.xlsx', 'bad.docx', 'bad.png']:
            response=self.client.post('/extract/document-text',files={'file':(filename,b'corrupt')},headers={'X-API-Key':'synthetic-token'})
            self.assertEqual(response.status_code,422)
        response=self.client.post('/extract/document-text',files={'file':('bad.doc',b'corrupt')},headers={'X-API-Key':'synthetic-token'})
        self.assertIn(response.status_code,(422,503))

    def test_tracked_ollama_preserves_response_without_live_model(self):
        from unittest.mock import patch,MagicMock
        response=MagicMock();response.__enter__.return_value=response
        # Construct JSON with the same message.content contract as Ollama.
        import json
        response.read.return_value=json.dumps({'message':{'content':'{"records":[]}'},'done':True}).encode()
        with patch('progress.Reporter.report') as report,patch('urllib.request.urlopen',return_value=response) as opener:
            result=self.client.post('/ollama/chat',headers={'X-API-Key':'synthetic-token'},json={
                'batch_id':1,'worker_token':'a'*32,'source_page':1,'pages_total':2,
                'ollama_request':{'model':'synthetic','stream':False,'messages':[]}})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['message']['content'],'{"records":[]}')
            self.assertEqual(report.call_args_list[-1].args,('llm',2,1))
            self.assertEqual(opener.call_args.args[0].full_url,'http://ollama_ai:11434/api/chat')

    def test_cancelled_document_never_reaches_ollama(self):
        from unittest.mock import patch
        from fastapi import HTTPException
        with patch('progress.Reporter.report',side_effect=HTTPException(409,'cancelled')),patch('urllib.request.urlopen') as opener:
            result=self.client.post('/ollama/chat',headers={'X-API-Key':'synthetic-token'},json={
                'batch_id':1,'worker_token':'a'*32,'source_page':1,'pages_total':2,
                'ollama_request':{'model':'synthetic','stream':False,'messages':[]}})
            self.assertEqual(result.status_code,409)
            opener.assert_not_called()

    @unittest.skipUnless(shutil.which('tesseract'), 'Tesseract rus+eng is required for real OCR tests')
    def test_image_and_scanned_pdf(self):
        from PIL import Image, ImageDraw
        import fitz
        image=Image.new('RGB',(1800,400),'white')
        draw=ImageDraw.Draw(image)
        draw.text((40,100),'SYNTHETIC INVESTOR 150000000',fill='black',font_size=55)
        buf=io.BytesIO();image.save(buf,format='PNG')
        self.assertIn('SYNTHETIC',self.extract('synthetic.png',buf.getvalue())['pages'][0]['raw_text'].upper())
        doc=fitz.open();page=doc.new_page(width=900,height=200)
        page.insert_image(page.rect,stream=buf.getvalue())
        self.assertIn('SYNTHETIC',self.extract('scan.pdf',doc.tobytes())['pages'][0]['raw_text'].upper())
        doc.close()
