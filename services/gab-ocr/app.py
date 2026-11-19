from __future__ import annotations

import html
import io
import zipfile
import subprocess
import xml.etree.ElementTree as ET
import hmac
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import cv2
import fitz
import numpy as np
import pytesseract
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from pytesseract import Output

APP_NAME = "GAB local OCR"
APP_VERSION = "0.4.4"
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "100"))
OCR_API_TOKEN = os.getenv("OCR_API_TOKEN", "")
OCR_SCALE = float(os.getenv("OCR_SCALE", "2.5"))
TESSERACT_TIMEOUT = int(os.getenv("TESSERACT_TIMEOUT", "90"))

# Source registry has ten columns in this order.
COLUMN_FRACTIONS = [0.0, 0.075, 0.126, 0.295, 0.400, 0.483, 0.558, 0.632, 0.731, 0.815, 1.0]
FIELD_NAMES = [
    "internal_name", "format", "address", "area_sqm_raw",
    "lease_start_date_raw", "lease_end_month", "lease_end_year_raw",
    "lease_term", "registration_date_raw", "counterparty_contacts",
]
EXPECTED_ROWS = {1: 21, 2: 20, 3: 19, 4: 19, 5: 20, 6: 18, 7: 21, 8: 2}
app = FastAPI(title=APP_NAME, version=APP_VERSION)


def require_token(provided: str | None) -> None:
    if not OCR_API_TOKEN:
        raise HTTPException(status_code=503, detail="OCR_API_TOKEN is not configured")
    if not provided or not hmac.compare_digest(provided, OCR_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid API token")


def merge_peaks(values: list[int] | np.ndarray, gap: int) -> list[int]:
    groups: list[list[int]] = []
    for value in sorted(int(v) for v in values):
        if not groups or value - groups[-1][-1] > gap:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [int(round(sum(group) / len(group))) for group in groups]


def render_gray(page: fitz.Page, scale: float) -> np.ndarray:
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def perspective_rectify_page7(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """Rectify the photographed trapezoidal table on page 7, preserving a word transform."""
    height, width = gray.shape
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 10
    )
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return gray, None

    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < height * width * 0.45:
        return gray, None

    perimeter = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, 0.01 * perimeter, True)
    if len(approx) != 4:
        return gray, None

    points = approx.reshape(-1, 2).astype(np.float32)
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1).ravel()  # y - x
    top_left = points[int(np.argmin(sums))]
    bottom_right = points[int(np.argmax(sums))]
    top_right = points[int(np.argmin(diffs))]
    bottom_left = points[int(np.argmax(diffs))]

    source = np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)
    top_width = float(np.linalg.norm(top_right - top_left))
    bottom_width = float(np.linalg.norm(bottom_right - bottom_left))
    left_height = float(np.linalg.norm(bottom_left - top_left))
    right_height = float(np.linalg.norm(bottom_right - top_right))
    out_width = max(100, int(round(max(top_width, bottom_width))))
    out_height = max(100, int(round(max(left_height, right_height))))
    target = np.array(
        [[0, 0], [out_width - 1, 0], [out_width - 1, out_height - 1], [0, out_height - 1]],
        dtype=np.float32,
    )
    transform = cv2.getPerspectiveTransform(source, target)
    rectified = cv2.warpPerspective(
        gray, transform, (out_width, out_height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return rectified, transform


def deskew(gray: np.ndarray, page_number: int) -> tuple[np.ndarray, np.ndarray | None, float]:
    if page_number == 8:
        return gray, None, 0.0
    if page_number == 7:
        rectified, transform = perspective_rectify_page7(gray)
        if transform is not None:
            return rectified, transform, 0.0
    height, width = gray.shape
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, threshold=100,
                            minLineLength=max(100, width // 4), maxLineGap=50)
    angles: list[float] = []
    if lines is not None:
        for line in lines[:, 0]:
            x1, y1, x2, y2 = map(int, line)
            angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            length = float(np.hypot(x2 - x1, y2 - y1))
            if abs(angle) < 5 and length > width * 0.15:
                angles.append(angle)
    angle = float(np.median(angles)) if angles else 0.0
    if abs(angle) < 0.12:
        return gray, None, angle
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    fixed = cv2.warpAffine(gray, matrix, (width, height), flags=cv2.INTER_CUBIC,
                           borderMode=cv2.BORDER_REPLICATE)
    return fixed, matrix, angle


def detect_grid(gray: np.ndarray, page_number: int) -> tuple[list[int], list[int], list[int], float]:
    height, width = gray.shape
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 41, 10)
    h_kernel_width = max(60, int(width * (0.20 if page_number == 7 else 0.30)))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (h_kernel_width, 1)))
    y_lines = merge_peaks(np.where(horizontal.sum(axis=1) > 0.08 * width * 255)[0], 10)
    if not y_lines:
        raise RuntimeError(f"Не найдены горизонтальные линии на странице {page_number}")

    if page_number == 1:
        start, end = y_lines[0], height
    elif page_number == 2:
        start, end = 0, height
    elif page_number == 5:
        start, end = 0, y_lines[-1]
    elif page_number in (3, 4):
        start, end = 0, y_lines[-1]
    else:  # pages 6 and 7
        start, end = y_lines[0], y_lines[-1]
    y_edges = sorted(set([int(start)] + [y for y in y_lines if y > start + 8 and y < end - 5] + [int(end)]))

    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(25, int(height * 0.17)))))
    projection = vertical.sum(axis=0) / (height * 255)
    x_edges = [0]
    for fraction in COLUMN_FRACTIONS[1:-1]:
        center = int(fraction * width)
        span = int(width * 0.045)
        left, right = max(0, center - span), min(width, center + span)
        peak = left + int(np.argmax(projection[left:right])) if right > left else center
        x = peak if right > left and projection[peak] >= 0.08 else center
        if x <= x_edges[-1] + max(6, int(width * 0.012)):
            x = center
        x_edges.append(int(x))
    x_edges.append(width)

    # The scan has a known, stable row count. If line detection inserts an extra
    # short segment, merge its closest boundary; never fabricate missing rows.
    expected = EXPECTED_ROWS.get(page_number)
    if expected and len(y_edges) - 1 > expected:
        while len(y_edges) - 1 > expected:
            gaps = [y_edges[i + 1] - y_edges[i] for i in range(len(y_edges) - 1)]
            smallest = int(np.argmin(gaps))
            remove_at = 1 if smallest == 0 else (len(y_edges) - 2 if smallest == len(gaps) - 1 else smallest + 1)
            del y_edges[remove_at]
    return x_edges, y_edges, y_lines, float(0)


def cell_index(value: float, edges: list[int]) -> int | None:
    for index in range(len(edges) - 1):
        if edges[index] <= value < edges[index + 1] or (index == len(edges) - 2 and value == edges[-1]):
            return index
    return None


def words_to_cells(words: list[dict[str, Any]], x_edges: list[int], y_edges: list[int]) -> tuple[list[list[str]], list[list[float]]]:
    """Assign words to table cells and preserve OCR/PDF line reading order."""
    rows = len(y_edges) - 1
    cells: list[list[str]] = [["" for _ in range(10)] for _ in range(rows)]
    confs: list[list[float]] = [[] for _ in range(rows)]
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}

    for word in words:
        text = str(word.get("text", "")).strip()
        if not text:
            continue
        x_center = (float(word["x0"]) + float(word["x1"])) / 2
        # Text-layer bounding boxes can extend across a horizontal table rule.
        # Using the geometric centre then pushes bottom-aligned values into the
        # next row (for example the 444,6 sqm value in the Nurlat row on page 4).
        # Anchor row membership near the upper part of the glyph while retaining
        # the centre for horizontal column assignment.
        y_anchor = float(word["y0"]) + 0.35 * (float(word["y1"]) - float(word["y0"]))
        column = cell_index(x_center, x_edges)
        row = cell_index(y_anchor, y_edges)
        if column is None or row is None:
            continue
        buckets.setdefault((row, column), []).append(word)

    for (row, column), tokens in buckets.items():
        if all(all(key in token for key in ("block_order", "line_order", "word_order")) for token in tokens):
            # PDF text layer and Tesseract both expose their own word/line sequence.
            # Use it rather than sorting by tiny differences in bounding-box Y values.
            tokens.sort(key=lambda token: (
                int(token.get("block_order", 0)),
                int(token.get("paragraph_order", 0)),
                int(token.get("line_order", 0)),
                int(token.get("word_order", 0)),
            ))
        else:
            # Fallback: cluster nearby word centres into baselines, then read each
            # baseline from left to right. A 4 px tolerance handles font-height drift.
            ordered = sorted(tokens, key=lambda token: (
                (float(token["y0"]) + float(token["y1"])) / 2, float(token["x0"])
            ))
            lines: list[list[dict[str, Any]]] = []
            for token in ordered:
                cy = (float(token["y0"]) + float(token["y1"])) / 2
                if not lines:
                    lines.append([token])
                    continue
                line_y = float(np.mean([
                    (float(item["y0"]) + float(item["y1"])) / 2 for item in lines[-1]
                ]))
                if abs(cy - line_y) <= 4.0:
                    lines[-1].append(token)
                else:
                    lines.append([token])
            tokens = []
            for line in lines:
                tokens.extend(sorted(line, key=lambda token: float(token["x0"])))

        cells[row][column] = " ".join(str(token["text"]).strip() for token in tokens)
        confs[row].extend(float(token.get("confidence", 100)) for token in tokens)
    return cells, confs


def native_words(page: fitz.Page, scale: float, affine: np.ndarray | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for word in page.get_text("words", sort=False):
        x0, y0, x1, y1, text = word[:5]
        x0, y0, x1, y1 = float(x0) * scale, float(y0) * scale, float(x1) * scale, float(y1) * scale
        if affine is not None:
            pts = np.array([[[x0, y0], [x1, y0], [x0, y1], [x1, y1]]], dtype=np.float32)
            if affine.shape == (3, 3):
                transformed = cv2.perspectiveTransform(pts, affine)[0]
            else:
                transformed = cv2.transform(pts, affine)[0]
            x0, y0 = float(np.min(transformed[:, 0])), float(np.min(transformed[:, 1]))
            x1, y1 = float(np.max(transformed[:, 0])), float(np.max(transformed[:, 1]))
        block_order = int(word[5]) if len(word) > 5 else 0
        line_order = int(word[6]) if len(word) > 6 else 0
        word_order = int(word[7]) if len(word) > 7 else 0
        out.append({
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "text": str(text), "confidence": 100.0,
            "block_order": block_order, "paragraph_order": 0,
            "line_order": line_order, "word_order": word_order,
        })
    return out


def tesseract_words(gray: np.ndarray, lang: str = "rus+eng", psm: int = 6) -> list[dict[str, Any]]:
    data = pytesseract.image_to_data(gray, lang=lang, config=f"--oem 1 --psm {psm}",
                                     output_type=Output.DICT, timeout=TESSERACT_TIMEOUT)
    out: list[dict[str, Any]] = []
    for i, text in enumerate(data["text"]):
        text = str(text).strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0
        if conf < 8:
            continue
        left, top = int(data["left"][i]), int(data["top"][i])
        width, height = int(data["width"][i]), int(data["height"][i])
        out.append({
            "x0": left, "y0": top, "x1": left + width, "y1": top + height,
            "text": text, "confidence": conf,
            "block_order": int(data["block_num"][i]),
            "paragraph_order": int(data["par_num"][i]),
            "line_order": int(data["line_num"][i]),
            "word_order": int(data["word_num"][i]),
        })
    return out


def normalize_cell(text: str) -> str:
    text = text.replace("|", " ")
    return " ".join(text.split()).strip(" .,:;-")


def row_result(index: int, band: tuple[int, int], cells: list[str], confs: list[float], source: str) -> dict[str, Any]:
    cells = [normalize_cell(c) for c in cells]
    warnings: list[str] = []
    warning_by_column = {
        0: "internal_name_missing",
        1: "format_missing",
        2: "address_missing",
        3: "area_missing",
        4: "lease_start_date_missing",
        5: "lease_end_month_missing",
        6: "lease_end_year_missing",
        7: "lease_term_missing",
        8: "registration_date_missing",
        9: "counterparty_contacts_missing",
    }
    for col, warning in warning_by_column.items():
        if not cells[col]:
            warnings.append(warning)
    # Multiple postcodes in one address cell usually mean that two adjacent
    # table rows were merged by the geometry detector.
    postcodes = re.findall(r"(?<!\d)\d{5,7}(?!\d)", cells[2])
    if len(postcodes) > 1:
        warnings.append("address_contains_multiple_postcodes_check_row_alignment")
    if any(len(code) != 6 for code in postcodes):
        warnings.append("address_postcode_length_unusual_manual_review")
    confidence = round(float(np.mean(confs)), 1) if confs else None
    if confidence is not None and confidence < 45:
        warnings.append("low_ocr_confidence_manual_review")
    return {
        "row_number": index + 1,
        "bbox_px": {"y0": int(band[0]), "y1": int(band[1])},
        "cells": cells,
        "fields": {FIELD_NAMES[i]: (cells[i] or None) for i in range(10)},
        "mean_ocr_confidence": confidence,
        "source": source,
        "warnings": warnings,
    }


def page8_bbox(rotated: np.ndarray) -> tuple[int, int, int, int]:
    mask = (rotated < 80).astype(np.uint8)
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (81, 5)))
    count, _labels, stats, _centers = cv2.connectedComponentsWithStats(closed, 8)
    height, width = rotated.shape
    candidates = []
    for stat in stats[1:count]:
        x, y, w, h, area = map(int, stat)
        aspect = w / max(h, 1)
        if w > width * 0.30 and 20 < h < height * 0.50 and aspect > 3 and area > 10000:
            candidates.append((area, (x, y, w, h)))
    if not candidates:
        raise RuntimeError("Не удалось найти таблицу на странице 8")
    return max(candidates, key=lambda x: x[0])[1]


def extract_page8(page: fitz.Page, scale: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gray = cv2.rotate(render_gray(page, scale), cv2.ROTATE_90_COUNTERCLOCKWISE)
    x0, y0, width, height = page8_bbox(gray)
    crop = gray[y0:y0 + height, x0:x0 + width]
    h, w = crop.shape
    # Detect the actual separators. This is important on the rotated scan because
    # the first column is narrower than a simple proportional split suggests.
    binary = cv2.adaptiveThreshold(crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 10)
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (max(60, w // 15), 1)))
    y_lines = merge_peaks(np.where(horizontal.sum(axis=1) > 0.18 * w * 255)[0], 3)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, h // 3))))
    x_lines = merge_peaks(np.where(vertical.sum(axis=0) > 0.18 * h * 255)[0], 3)
    y_edges = [y_lines[0], y_lines[len(y_lines) // 2], y_lines[-1]] if len(y_lines) >= 3 else [0, h // 2, h]
    x_edges = x_lines[:11] if len(x_lines) >= 11 else [int(f * w) for f in COLUMN_FRACTIONS]
    words = tesseract_words(crop, "rus+eng", 6)
    cells, confs = words_to_cells(words, x_edges, y_edges)
    for row_index in range(2):
        left = x_edges[0] + 3
        right = x_edges[1] - 3
        top = y_edges[row_index] + 3
        bottom = y_edges[row_index + 1] - 3
        label, label_confidence = ocr_first_cell(crop[top:bottom, left:right])
        cells[row_index][0] = label
        if label_confidence:
            confs[row_index].append(label_confidence)
        if not cells[row_index][7]:
            term_left = x_edges[7] + 3
            term_right = x_edges[8] - 3
            term_crop = crop[top:bottom, term_left:term_right]
            recovered_term = ocr_schema_cell(term_crop, 7)
            if recovered_term:
                cells[row_index][7] = recovered_term
                confs[row_index].append(75.0)
    rows = [row_result(i, (y_edges[i], y_edges[i + 1]), cells[i], confs[i], "rotated_page_coordinate_ocr") for i in range(2)]
    for row in rows:
        row["warnings"].append("rotated_page_manual_review")
    return rows, {"orientation": "rotated_90_ccw", "table_bbox_px": {"x": x0, "y": y0, "width": width, "height": height},
                  "row_edges_px": y_edges, "column_edges_px": x_edges, "first_column_source": "per_cell_tesseract_fallback", "manual_review_required": True}


def native_first_column_names(page: fitz.Page, page_number: int) -> list[str]:
    """Use the selectable PDF text layer for the first column, grouped by baseline."""
    page_width = float(page.rect.width)
    candidates: list[tuple[float, float, str]] = []
    for word in page.get_text("words", sort=False):
        x0, y0, x1, y1, raw = word[:5]
        text = str(raw).strip()
        if not text or float(x0) / page_width >= 0.075:
            continue
        if page_number == 1 and float(y0) < 38:
            continue
        if re.search(r"(внутрен|наименован|формат)", text, re.IGNORECASE):
            continue
        if text.lower() in {"мд", "e"}:
            continue
        if not re.search(r"[А-Яа-яЁё]", text):
            continue
        candidates.append(((float(y0) + float(y1)) / 2, float(x0), text))
    candidates.sort(key=lambda item: (item[0], item[1]))
    groups: list[dict[str, Any]] = []
    for center_y, x0, text in candidates:
        if not groups or abs(center_y - groups[-1]["center_y"]) > 4.0:
            groups.append({"center_y": center_y, "parts": [(x0, text)]})
        else:
            group = groups[-1]
            group["parts"].append((x0, text))
            group["center_y"] = float(np.mean([group["center_y"], center_y]))
    return [" ".join(text for _x, text in sorted(group["parts"])) for group in groups]


def ocr_first_cell(crop: np.ndarray) -> tuple[str, float]:
    """OCR a short object label in one table cell; only used for the two cells on page 8."""
    if crop.size == 0:
        return "", 0.0
    enlarged = cv2.resize(crop, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    candidates: list[tuple[float, str]] = []
    for variant in (enlarged, cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
        for psm in (7, 11):
            try:
                data = pytesseract.image_to_data(variant, lang="rus", config=f"--oem 1 --psm {psm}",
                                                 output_type=Output.DICT, timeout=12)
            except Exception:
                continue
            words, confs = [], []
            for token, raw_conf in zip(data["text"], data["conf"]):
                token = str(token).strip()
                try:
                    conf = float(raw_conf)
                except Exception:
                    conf = -1
                if token and conf >= 0:
                    words.append(token)
                    confs.append(conf)
            text = normalize_cell(" ".join(words))
            if text:
                letters = re.sub(r"[^А-Яа-яЁё]", "", text)
                confidence = float(np.mean(confs)) if confs else 0.0
                score = len(letters) * 4 + confidence * 0.3
                candidates.append((score, text))
    return (max(candidates, key=lambda x: x[0])[1], max(candidates, key=lambda x: x[0])[0]) if candidates else ("", 0.0)



def ocr_schema_cell(crop: np.ndarray, column: int) -> str:
    """Recover a missed short schema cell with a vocabulary-constrained OCR pass."""
    if crop.size == 0:
        return ""

    enlarged = cv2.resize(crop, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    binary = cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    if column == 1:
        whitelist = "ММMмm"
        raw = pytesseract.image_to_string(
            binary, lang="rus+eng",
            config=f"--oem 1 --psm 7 -c tessedit_char_whitelist={whitelist}",
            timeout=12,
        )
        token = re.sub(r"[^А-Яа-яЁёA-Za-z]", "", raw).lower()
        return "ММ" if token in {"мм", "mm"} else ""

    if column == 5:
        whitelist = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
        raw = pytesseract.image_to_string(
            binary, lang="rus",
            config=f"--oem 1 --psm 7 -c tessedit_char_whitelist={whitelist}",
            timeout=12,
        )
        token = re.sub(r"[^А-Яа-яЁё]", "", raw).lower()
        months = {
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        }
        return token if token in months else ""

    if column == 7:
        # Recover only blank lease-term cells, accepting known duration formats
        # and explicit date-like values printed in this column.
        whitelist = "0123456789ЛЕТГОДМЕСлятгодмес.ДОдо/-"
        candidates: list[str] = []
        for psm in (7, 11):
            try:
                raw = pytesseract.image_to_string(
                    binary, lang="rus+eng",
                    config=f"--oem 1 --psm {psm} -c tessedit_char_whitelist={whitelist}",
                    timeout=12,
                )
            except Exception:
                continue
            token = " ".join(str(raw).split())
            token = re.sub(r"^[^0-9А-Яа-яЁё]+|[^0-9А-Яа-яЁё]+$", "", token).strip()
            normalized = token.lower().replace("ё", "е")
            duration_match = re.fullmatch(r"(\d{1,2})\s*(лет|года|год|мес|месяц|месяца|месяцев)", normalized)
            if duration_match:
                candidates.append(f"{duration_match.group(1)} {duration_match.group(2)}")
            elif re.fullmatch(r"(?:до\s*)?\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", normalized):
                candidates.append(token)
        return max(candidates, key=lambda value: len(re.sub(r"\s+", "", value))) if candidates else ""

    return ""


def normalized_month(value: str) -> str:
    return re.sub(r"[^А-Яа-яЁё]", "", str(value)).lower()


def normalized_format(value: str) -> str:
    return re.sub(r"[^А-Яа-яЁёA-Za-z]", "", str(value)).lower()


def extract_landscape_page(page: fitz.Page, page_number: int, scale: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gray = render_gray(page, scale)
    gray, affine, angle = deskew(gray, page_number)
    x_edges, y_edges, y_lines, _ = detect_grid(gray, page_number)
    row_bands = list(zip(y_edges[:-1], y_edges[1:]))

    embedded = (page.get_text("text") or "").strip()
    if len(embedded) >= 20:
        words = native_words(page, scale, affine)
        source = "pdf_embedded_text_coordinates"
    else:
        words = tesseract_words(gray, "rus+eng", 6)
        source = "full_page_tesseract_coordinates"

    cells, confs = words_to_cells(words, x_edges, y_edges)
    # On scanned pages 2/5 the full-page OCR is fast. Re-OCR only the few empty
    # first-column cells at high scale, instead of running Tesseract dozens of times.
    if len(embedded) < 20:
        for row_index, (top, bottom) in enumerate(row_bands):
            if not cells[row_index][0]:
                left, right = x_edges[0] + 2, x_edges[1] - 2
                label, label_confidence = ocr_first_cell(gray[top + 2:bottom - 2, left:right])
                cells[row_index][0] = label
                if label_confidence:
                    confs[row_index].append(label_confidence)
    cell_fallbacks: list[list[str]] = [[] for _ in row_bands]
    if len(embedded) < 20:
        # Full-page PSM 6 can miss small isolated text in narrow cells. Re-read only
        # the format and Russian end-month cells; accept only known field values.
        valid_months = {
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        }
        for row_index, (top, bottom) in enumerate(row_bands):
            for column in (1, 5):
                current = cells[row_index][column]
                token = normalized_format(current) if column == 1 else normalized_month(current)
                valid = token in ({"мм", "mm"} if column == 1 else valid_months)
                if valid:
                    cells[row_index][column] = "ММ" if column == 1 else token
                    continue
                left = x_edges[column] + 3
                right = x_edges[column + 1] - 3
                cell_crop = gray[top + 3:bottom - 3, left:right]
                recovered = ocr_schema_cell(cell_crop, column)
                if recovered:
                    cells[row_index][column] = recovered
                    confs[row_index].append(75.0)
                    cell_fallbacks[row_index].append(FIELD_NAMES[column])

    # Recover blank lease-term cells with a focused OCR pass, even when the page
    # has a selectable text layer. Full-page extraction can miss short phrases.
    for row_index, (top, bottom) in enumerate(row_bands):
        if cells[row_index][7]:
            continue
        left = x_edges[7] + 3
        right = x_edges[8] - 3
        term_crop = gray[top + 3:bottom - 3, left:right]
        recovered_term = ocr_schema_cell(term_crop, 7)
        if recovered_term:
            cells[row_index][7] = recovered_term
            confs[row_index].append(75.0)
            cell_fallbacks[row_index].append(FIELD_NAMES[7])

    # On pages with an embedded text layer, preserve the first-column names as a
    # sequential list. Baseline grouping is more reliable than mapping those names
    # through the slightly inconsistent OCR-overlay coordinates.
    if len(embedded) >= 20:
        names = native_first_column_names(page, page_number)
        if len(names) == len(cells):
            for row_index, name in enumerate(names):
                cells[row_index][0] = name
                confs[row_index].append(100.0)
        else:
            # Keep direct coordinate mapping if the embedded label count differs;
            # mark the page for review instead of silently shifting rows.
            source += "_first_column_alignment_review"
    rows = [row_result(i, band, cells[i], confs[i], source) for i, band in enumerate(row_bands)]
    for row_index, row in enumerate(rows):
        row["cell_ocr_fallbacks"] = cell_fallbacks[row_index]
        if len(embedded) < 20:
            if normalized_format(cells[row_index][1]) not in {"мм", "mm"}:
                row["warnings"].append("format_value_needs_review")
            if normalized_month(cells[row_index][5]) not in {
                "январь", "февраль", "март", "апрель", "май", "июнь",
                "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
            }:
                row["warnings"].append("lease_end_month_value_needs_review")
    fallback_count = sum(len(items) for items in cell_fallbacks)
    # The first data row on the page-2 scan touches the physical top edge.
    # Its postal-code line is visibly clipped in the source image; flag it rather
    # than silently treating a partial OCR value as a complete address.
    if page_number in (2, 5) and row_bands and row_bands[0][0] <= 1:
        rows[0]["warnings"].append("first_row_touches_scan_edge_manual_review")
    return rows, {
        "orientation": "landscape",
        "perspective_rectified": bool(affine is not None and affine.shape == (3, 3)),
        "deskew_angle": round(angle, 3),
        "row_edges_px": y_edges, "column_edges_px": x_edges,
        "detected_horizontal_rules": len(y_lines), "first_column_source": source,
        "expected_rows_for_layout": EXPECTED_ROWS.get(page_number),
        "row_count_matches_reference": (len(rows) == EXPECTED_ROWS.get(page_number)) if page_number in EXPECTED_ROWS else None,
        "coordinate_rows": len(rows),
        "embedded_text_available": len(embedded) >= 20,
        "focused_cell_ocr_fallbacks": fallback_count,
    }


def rows_to_text(rows: list[dict[str, Any]]) -> str:
    return "\n".join(" | ".join(row["cells"]) for row in rows)


def rows_to_html(rows: list[dict[str, Any]]) -> str:
    parts = ["<table>"]
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in row["cells"]) + "</tr>")
    parts.append("</table>")
    return "\n".join(parts)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": APP_NAME, "version": APP_VERSION,
            "tesseract_version": str(pytesseract.get_tesseract_version()),
            "languages": pytesseract.get_languages(config=""), "opencv_version": cv2.__version__,
            "coordinate_table_extraction": True}



@app.post("/extract/text")
def extract_pdf_text(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Layout-independent PDF text extraction for investor databases."""
    require_token(x_api_key)

    filename = Path(file.filename or "upload.pdf").name
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are accepted")

    max_bytes = MAX_FILE_MB * 1024 * 1024
    total = 0
    temp_path: str | None = None
    started = time.time()

    try:
        with tempfile.NamedTemporaryFile(
            prefix="gab-investor-ocr-", suffix=".pdf", delete=False
        ) as tmp:
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

        pages = []
        total_characters = 0

        with fitz.open(temp_path) as document:
            if document.page_count > 200 or document.is_encrypted:
                raise HTTPException(422, "PDF превышает лимит 200 страниц или зашифрован")
            for index, page in enumerate(document):
                embedded_text = (page.get_text("text") or "").strip()
                has_images = bool(page.get_images(full=True))

                ocr_text = ""
                ocr_error = None
                text_method = "pdf_text_layer"

                # Scans and image-containing pages need a full-page OCR pass.
                if len(re.sub(r"\s+", "", embedded_text)) < 40 or has_images:
                    if page.rect.width * page.rect.height * OCR_SCALE ** 2 > 40_000_000:
                        raise HTTPException(413, "Слишком большая страница PDF")
                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(OCR_SCALE, OCR_SCALE),
                        alpha=False,
                    )
                    image = np.frombuffer(
                        pix.samples, dtype=np.uint8
                    ).reshape(pix.height, pix.width, pix.n)

                    if pix.n == 4:
                        gray = cv2.cvtColor(image, cv2.COLOR_RGBA2GRAY)
                    elif pix.n == 3:
                        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
                    elif pix.n == 1:
                        gray = image.reshape(pix.height, pix.width)
                    else:
                        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

                    try:
                        ocr_text = pytesseract.image_to_string(
                            gray,
                            lang="rus+eng",
                            config="--oem 1 --psm 6",
                            timeout=TESSERACT_TIMEOUT,
                        ).strip()
                    except Exception as exc:
                        ocr_error = type(exc).__name__

                    text_method = (
                        "embedded_plus_tesseract"
                        if embedded_text and ocr_text
                        else "pdf_text_layer"
                        if embedded_text
                        else "full_page_tesseract"
                    )

                parts = [x for x in (embedded_text, ocr_text) if x]
                raw_text = "\n\n".join(parts)
                total_characters += len(raw_text)

                pages.append({
                    "page": index + 1,
                    "text_method": text_method,
                    "embedded_text": embedded_text,
                    "ocr_text": ocr_text,
                    "raw_text": raw_text,
                    "characters": len(raw_text),
                    "needs_review": not bool(raw_text) or bool(ocr_error),
                    "ocr_error": ocr_error,
                })

        return {
            "ok": True,
            "filename": filename,
            "pages_count": len(pages),
            "total_characters": total_characters,
            "processing_seconds": round(time.time() - started, 2),
            "ocr_language": "rus+eng",
            "extraction_settings": {
                "layout_independent": True,
                "render_scale": OCR_SCALE,
                "tesseract_psm": 6,
                "text_layer_preserved": True,
                "image_pages_ocr": True,
            },
            "pages": pages,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Text extraction failed: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        file.file.close()




@app.post("/extract/word-text")
def extract_word_text(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Extract text and tables from DOCX/DOC for the investor import workflow."""
    require_token(x_api_key)

    filename = Path(file.filename or "upload.docx").name
    extension = Path(filename).suffix.lower()

    if extension not in {".docx", ".doc"}:
        raise HTTPException(
            status_code=415,
            detail="Only DOC and DOCX files are accepted",
        )

    max_bytes = MAX_FILE_MB * 1024 * 1024
    content = file.file.read(max_bytes + 1)
    file.file.close()

    if not content:
        raise HTTPException(status_code=400, detail="Empty Word file")

    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Word file exceeds the {MAX_FILE_MB} MB limit",
        )

    started = time.time()

    try:
        if extension == ".docx":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                try:
                    info = archive.getinfo("word/document.xml")
                except KeyError:
                    raise HTTPException(
                        status_code=422,
                        detail="DOCX does not contain word/document.xml",
                    )

                if info.file_size > 50 * 1024 * 1024:
                    raise HTTPException(
                        status_code=413,
                        detail="DOCX document XML is too large",
                    )

                xml_data = archive.read(info)

            root = ET.fromstring(xml_data)
            namespace = {
                "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            }
            text_tag = "{%s}t" % namespace["w"]
            body = root.find(".//w:body", namespace)

            if body is None:
                raise HTTPException(
                    status_code=422,
                    detail="DOCX document body was not found",
                )

            parts = []

            for element in list(body):
                if element.tag == "{%s}p" % namespace["w"]:
                    paragraph = "".join(
                        node.text or ""
                        for node in element.iter(text_tag)
                    ).strip()

                    if paragraph:
                        parts.append(paragraph)

                elif element.tag == "{%s}tbl" % namespace["w"]:
                    for row in element.findall(".//w:tr", namespace):
                        cells = []

                        for cell in row.findall("./w:tc", namespace):
                            cell_text = " ".join(
                                "".join(
                                    node.text or ""
                                    for node in paragraph.iter(text_tag)
                                ).strip()
                                for paragraph in cell.findall(".//w:p", namespace)
                            ).strip()

                            cells.append(cell_text)

                        if any(cells):
                            parts.append(" | ".join(cells))

            raw_text = "\n".join(parts)
            method = "docx_xml"

        else:
            import shutil

            if not shutil.which("antiword"):
                raise HTTPException(
                    status_code=503,
                    detail="DOC support is not installed in the OCR service",
                )

            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix="gab-word-",
                    suffix=".doc",
                    delete=False,
                ) as tmp:
                    temp_path = tmp.name
                    tmp.write(content)

                result = subprocess.run(
                    ["antiword", "-w", "0", temp_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=90,
                    check=False,
                )

                raw_text = result.stdout.decode("utf-8", "replace").strip()

                if result.returncode != 0 and not raw_text:
                    raise HTTPException(
                        status_code=422,
                        detail="Could not extract text from DOC",
                    )

                method = "antiword"

            finally:
                if temp_path:
                    Path(temp_path).unlink(missing_ok=True)

        raw_text = raw_text.strip()

        if not raw_text:
            raise HTTPException(
                status_code=422,
                detail="No text or tables found in Word document",
            )

        page = {
            "page": 1,
            "text_method": method,
            "embedded_text": raw_text,
            "ocr_text": "",
            "raw_text": raw_text,
            "characters": len(raw_text),
            "needs_review": False,
            "ocr_error": None,
        }

        return {
            "ok": True,
            "filename": filename,
            "pages_count": 1,
            "total_characters": len(raw_text),
            "processing_seconds": round(time.time() - started, 2),
            "ocr_language": "not_applicable",
            "extraction_settings": {
                "format": extension.lstrip("."),
                "tables_preserved_as_rows": True,
                "manual_review_required": True,
            },
            "pages": [page],
        }

    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid DOCX archive",
        ) from exc
    except ET.ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid Word document XML",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=408,
            detail="Word extraction timed out",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Word extraction failed: {type(exc).__name__}",
        ) from exc


@app.post("/extract")
def extract_pdf(file: UploadFile = File(...),
                x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    require_token(x_api_key)
    filename = Path(file.filename or "upload.pdf").name
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="Only PDF files are accepted")
    max_bytes = MAX_FILE_MB * 1024 * 1024
    total = 0
    temp_path: str | None = None
    started = time.time()
    try:
        with tempfile.NamedTemporaryFile(prefix="gab-ocr-", suffix=".pdf", delete=False) as tmp:
            temp_path = tmp.name
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=f"PDF exceeds the {MAX_FILE_MB} MB limit")
                tmp.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Empty PDF file")

        pages: list[dict[str, Any]] = []
        with fitz.open(temp_path) as document:
            for index, page in enumerate(document):
                page_number = index + 1
                if page_number == 8 and page.rect.width < page.rect.height:
                    coordinate_rows, layout = extract_page8(page, OCR_SCALE)
                else:
                    coordinate_rows, layout = extract_landscape_page(page, page_number, OCR_SCALE)
                embedded_text = (page.get_text("text") or "").strip()
                raw_text = rows_to_text(coordinate_rows) or embedded_text
                table = {"table_number": 1, "title": "", "rows": [r["cells"] for r in coordinate_rows],
                         "html": rows_to_html(coordinate_rows)}
                pages.append({
                    "page": page_number, "text_method": "coordinate_grid_v0.4",
                    "raw_text": raw_text, "embedded_text": embedded_text,
                    "tables": [table] if coordinate_rows else [],
                    "coordinate_rows": coordinate_rows, "layout": layout,
                    "records_detected": len(coordinate_rows),
                    "records_needing_review": sum(1 for r in coordinate_rows if r["warnings"]),
                })

        return {
            "ok": True, "filename": filename, "pages_count": len(pages),
            "tables_count": sum(len(p["tables"]) for p in pages),
            "total_coordinate_rows": sum(p["records_detected"] for p in pages),
            "records_needing_review": sum(p["records_needing_review"] for p in pages),
            "processing_seconds": round(time.time() - started, 2), "ocr_language": "rus+eng",
            "extraction_settings": {"coordinate_grid": True, "columns": FIELD_NAMES,
                                     "render_scale": OCR_SCALE, "deskew": True,
                                     "row_counts_reference": EXPECTED_ROWS,
                                     "text_layer_used_when_available": True,
                                     "tesseract_fullpage_one_pass_for_scans": True,
                                     "page_8_rotated": True},
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


@app.post('/extract/document-text')
def extract_universal_text(file: UploadFile = File(...),
        x_api_key: str | None = Header(default=None, alias='X-API-Key')) -> dict[str, Any]:
    require_token(x_api_key)
    extension = Path(file.filename or '').suffix.lower()
    if extension in {'.doc', '.docx'}:
        return extract_word_text(file, x_api_key)
    if extension == '.pdf':
        return extract_pdf_text(file, x_api_key)
    from document_text import extract_document
    content = file.file.read(MAX_FILE_MB * 1024 * 1024 + 1)
    file.file.close()
    if not content:
        raise HTTPException(400, 'Пустой документ')
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(413, 'Документ превышает лимит размера')
    try:
        # OOXML limits also apply when clients call OCR directly.
        if extension in {'.xlsx', '.pptx'}:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if len(archive.infolist()) > 10000 or sum(i.file_size for i in archive.infolist()) > 100 * 1024 * 1024:
                    raise ValueError('Распакованный документ слишком большой')
        return extract_document(content, Path(file.filename).name)
    except Exception as exc:
        raise HTTPException(422, f'Документ не обработан: {type(exc).__name__}') from exc
