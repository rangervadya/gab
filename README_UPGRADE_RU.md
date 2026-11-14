# GAB Local OCR v0.4.0

## Что меняется

- Сохраняется прежний API: `POST /extract`, multipart-поле `file`, заголовок `X-API-Key`.
- `GET /health` дополнен версией и сведениями об OpenCV.
- PDF обрабатывается локально.
- Страницы с текстовым слоем используют координаты встроенных слов PDF.
- Сканированные страницы распознаются одним проходом Tesseract с распределением слов по ячейкам.
- Повёрнутая восьмая страница разворачивается и обрабатывается отдельно.
- В ответ добавлены `coordinate_rows`, `layout`, `records_detected`, `records_needing_review`, `total_coordinate_rows`, `processing_seconds`.
- Старые поля `pages[].raw_text`, `pages[].tables[].rows`, `pages[].tables[].html` сохранены для совместимости с текущим n8n.

## Важное

Перед внедрением обязательно сохранить резервные копии текущих `/opt/gab/app.py`, `/opt/gab/requirements.txt`, `/opt/gab/Dockerfile`.
После внедрения протестировать `/health`, затем загрузку контрольного PDF через существующий Form Trigger. Не отправлять исходный PDF в GitHub или внешние сервисы.

## Docker

Контейнер должен собираться из `/opt/gab` с командой:

```bash
cd /root/ai-stack
docker compose build gab_ocr
docker compose up -d --no-deps gab_ocr
```

Сервис продолжает использовать переменную `OCR_API_TOKEN` из существующего `/root/ai-stack/.env`.
