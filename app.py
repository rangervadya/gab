import hmac
import os
import tempfile
from pathlib import Path
from typing import Any

import fitz
import pytesseract
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from PIL import Image
from img2table.document import PDF
from img2table.ocr import TesseractOCR

APP_NAME = "GAB local OCR"
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "100"))
OCR_API_TOKEN = os.getenv("OCR_API_TOKEN", "")

app = FastAPI(title=APP_NAME, version="0.1.0")


def require_token(provided: str | None) -> None:
    if not OCR_API_TOKEN:
        raise HTTPException(status_code=503, detail="OCR_API_TOKEN is not configured")
    if not provided or not hmac.compare_digest(provided, OCR_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid API token")


def dataframe_rows(table: Any) -> list[list[str]]:
    df = table.df.fillna("").astype(str)
    return [[str(value).strip() for value in row] for row in df.values.tolist()]


def get_page_text(pdf_path: str) -> list[dict[str, Any]]:
    """Return embedded text where available, otherwise local Russian/English OCR."""
    result: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            text = (page.get_text("text") or "").strip()
            method = "embedded_text"
            if len(text) < 20:
                pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text = pytesseract.image_to_string(
                    image,
                    lang="rus+eng",
                    config="--oem 1 --psm 6",
                ).strip()
                method = "tesseract_full_page"
            result.append({"page": page_index + 1, "method": method, "text": text})
    return result


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": APP_NAME,
        "tesseract_version": str(pytesseract.get_tesseract_version()),
        "languages": pytesseract.get_languages(config=""),
    }


@app.post("/extract")
def extract_pdf(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    require_token(x_api_key)

    filename = Path(file.filename or "upload.pdf").name
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are accepted")

    max_bytes = MAX_FILE_MB * 1024 * 1024
    total = 0
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="gab-ocr-", suffix=".pdf", delete=False) as tmp:
            temp_path = tmp.name
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"PDF exceeds the {MAX_FILE_MB} MB limit",
                    )
                tmp.write(chunk)

        if total == 0:
            raise HTTPException(status_code=400, detail="Empty PDF file")

        # Full-page OCR is returned as a fallback even if table detection misses a grid.
        page_text = get_page_text(temp_path)

        ocr = TesseractOCR(n_threads=1, lang="rus+eng", psm=11)
        pdf = PDF(src=temp_path, detect_rotation=True, pdf_text_extraction=True)
        extracted = pdf.extract_tables(
            ocr=ocr,
            implicit_rows=True,
            implicit_columns=True,
            borderless_tables=True,
            min_confidence=35,
            max_workers=1,
        )

        pages: list[dict[str, Any]] = []
        page_count = len(page_text)
        for index in range(page_count):
            table_items = extracted.get(index, [])
            tables = []
            for table_index, table in enumerate(table_items, start=1):
                tables.append(
                    {
                        "table_number": table_index,
                        "title": getattr(table, "title", "") or "",
                        "rows": dataframe_rows(table),
                        "html": getattr(table, "html", "") or "",
                    }
                )
            text_item = page_text[index]
            pages.append(
                {
                    "page": index + 1,
                    "text_method": text_item["method"],
                    "raw_text": text_item["text"],
                    "tables": tables,
                }
            )

        return {
            "ok": True,
            "filename": filename,
            "pages_count": page_count,
            "tables_count": sum(len(page["tables"]) for page in pages),
            "ocr_language": "rus+eng",
            "extraction_settings": {
                "implicit_rows": True,
                "implicit_columns": True,
                "borderless_tables": True,
                "min_confidence": 35,
            },
            "pages": pages,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OCR failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        file.file.close()
