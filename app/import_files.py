"""Container/signature validation, without parsing document contents for CRM."""
import io
import zipfile

MIME_TYPES = {
    'pdf': {'application/pdf'},
    'doc': {'application/msword', 'application/x-ole-storage'},
    'xls': {'application/vnd.ms-excel', 'application/x-ole-storage'},
    'docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
    'xlsx': {'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},
    'pptx': {'application/vnd.openxmlformats-officedocument.presentationml.presentation'},
    'jpg': {'image/jpeg'}, 'jpeg': {'image/jpeg'}, 'png': {'image/png'},
    'tif': {'image/tiff'}, 'tiff': {'image/tiff'},
    'csv': {'text/csv', 'application/csv', 'text/plain', 'application/vnd.ms-excel'},
}


def validate_file(extension, content, mime_type):
    if extension not in MIME_TYPES:
        raise ValueError('Неподдерживаемый формат документа')
    mime_type = (mime_type or 'application/octet-stream').split(';')[0].strip().lower()
    if mime_type not in MIME_TYPES[extension] | {'application/octet-stream'}:
        raise ValueError('MIME-тип не соответствует расширению файла')
    signatures = {'pdf': b'%PDF-', 'doc': bytes.fromhex('d0cf11e0a1b11ae1'),
                  'xls': bytes.fromhex('d0cf11e0a1b11ae1'), 'png': b'\x89PNG\r\n\x1a\n',
                  'jpg': b'\xff\xd8\xff', 'jpeg': b'\xff\xd8\xff'}
    if extension in signatures and not content.startswith(signatures[extension]):
        raise ValueError('Содержимое файла не соответствует расширению')
    if extension in {'tif', 'tiff'} and not content.startswith((b'II*\x00', b'MM\x00*')):
        raise ValueError('Некорректная сигнатура TIFF')
    if extension in {'docx', 'xlsx', 'pptx'}:
        required = {'docx':'word/document.xml', 'xlsx':'xl/workbook.xml', 'pptx':'ppt/presentation.xml'}[extension]
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                if len(infos) > 10000 or sum(i.file_size for i in infos) > 100 * 1024 * 1024:
                    raise ValueError('Распакованный документ превышает допустимый размер')
                if required not in archive.namelist() or any(i.flag_bits & 1 for i in infos):
                    raise ValueError('Документ повреждён или зашифрован')
                if archive.testzip():
                    raise ValueError('Повреждённый архив документа')
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ValueError('Повреждённый архив документа') from exc
    if extension == 'csv' and b'\x00' in content:
        raise ValueError('CSV должен быть текстовым файлом')
