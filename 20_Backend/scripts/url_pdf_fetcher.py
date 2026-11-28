#!/usr/bin/env python3
"""Fetch PDFs from a URL (including paginated HTML listings and browser viewers) and merge into one PDF."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import signal
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageChops, ImageOps, ImageStat
from pypdf import PdfReader, PdfWriter

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

NEXT_TEXT_PATTERNS = (
    re.compile(r"^\s*(次へ|次のページ|次ページ|次の頁|next|next\s*page)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(>|›|»|→|▶)\s*$"),
    re.compile(r"(次へ|次のページ|next\s*page)", re.IGNORECASE),
)


def log(message: str) -> None:
    """Print a flushed progress message to stdout."""
    print(message, flush=True)


def is_http_url(url: str) -> bool:
    """Return True if the URL uses http or https."""
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_pdf_bytes(data: bytes) -> bool:
    """Return True if raw bytes begin with a PDF header."""
    return data.lstrip()[:5] == b"%PDF-"


def looks_like_pdf_url(url: str) -> bool:
    """Heuristic check for URLs that point to a PDF resource."""
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    if path_lower.endswith(".pdf"):
        return True
    return ".pdf" in parsed.query.lower()


def create_session() -> requests.Session:
    """Create a configured requests session."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def download_if_pdf(session: requests.Session, url: str, timeout: int) -> bytes | None:
    """Download a URL and return bytes if the response is a valid PDF."""
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        log(f"[WARN] Failed to fetch {url}: {exc}")
        return None


    content_type = response.headers.get("Content-Type", "").lower()
    data = response.content
    if "application/pdf" in content_type or is_pdf_bytes(data):
        return data
    return None



def extract_pdf_links_from_html(html: str | bytes, base_url: str) -> list[str]:
    """Extract candidate PDF URLs from HTML elements in document order."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(raw_url: str | None) -> None:
        if not raw_url:
            return
        absolute = urljoin(base_url, raw_url.strip())
        if not is_http_url(absolute):
            return
        if absolute not in seen and looks_like_pdf_url(absolute):
            seen.add(absolute)
            candidates.append(absolute)

    for tag in soup.find_all("a", href=True):
        add_candidate(tag.get("href"))
    for tag in soup.find_all("iframe", src=True):
        add_candidate(tag.get("src"))
    for tag in soup.find_all("embed", src=True):
        add_candidate(tag.get("src"))
    for tag in soup.find_all("object", data=True):
        add_candidate(tag.get("data"))

    return candidates


def find_next_page_url(html: str | bytes, current_url: str) -> str | None:
    """Find the next pagination URL on the same host, if present."""
    soup = BeautifulSoup(html, "html.parser")
    current_host = urlparse(current_url).netloc

    def normalize_same_host(raw_href: str | None) -> str | None:
        if not raw_href:
            return None

        candidate = urljoin(current_url, raw_href.strip())
        parsed = urlparse(candidate)
        if not is_http_url(candidate) or parsed.netloc != current_host:
            return None

        if candidate == current_url:
            return None

        return candidate

    # 1. Explicit rel="next"
    for tag in soup.find_all(["a", "link"], rel=True, href=True):
        rels = [r.lower() for r in (tag.get("rel") or [])]
        if "next" in rels:
            if resolved := normalize_same_host(tag.get("href")):
                return resolved

    # 2. Anchor text / aria-label / title matching next-page patterns
    anchors = soup.find_all("a", href=True)
    for pattern in NEXT_TEXT_PATTERNS:
        for tag in anchors:
            label = " ".join(
                filter(
                    None,
                    [
                        tag.get_text(" ", strip=True),
                        tag.get("aria-label"),
                        tag.get("title"),
                    ],
                )
            )
            if label and pattern.search(label):
                if resolved := normalize_same_host(tag.get("href")):
                    return resolved

    # 3. Numbered pagination inside a pagination container
    for container in soup.select("nav, .pagination, .pager, [class*='pagin'], [class*='pager']"):
        current_el = container.select_one("[aria-current='page'], .active, .current, strong, span.current")
        if not current_el:
            continue
        current_text = current_el.get_text(strip=True)
        if not current_text.isdigit():
            continue
        next_num = str(int(current_text) + 1)
        for tag in container.find_all("a", href=True):
            if tag.get_text(strip=True) == next_num:
                if resolved := normalize_same_host(tag.get("href")):
                    return resolved

    return None



def _infer_saved_viewport_width(captures: list) -> int:
    """Infer portrait (860) vs landscape (1360) viewport width from saved cover PDF."""
    if not captures:
        return READER_VIEWPORT_WIDTH
    try:
        import pymupdf

        with pymupdf.open(stream=captures[0].pdf, filetype="pdf") as doc:
            if len(doc) > 0:
                r = doc[0].rect
                if r.width > 100 and r.height > 100 and (r.height / r.width) < 1.15:
                    return READER_LANDSCAPE_VIEWPORT_WIDTH
    except Exception:
        pass
    return READER_VIEWPORT_WIDTH


def _content_bbox_stripping_letterbox(im: Image.Image) -> tuple[int, int, int, int] | None:
    """Peel uniform solid-white (>=245) or solid-black (<=15) outer letterbox bars from all 4 edges."""
    rgb = im.convert("RGB")
    w, h = rgb.size
    if w < 10 or h < 10:
        return None
    gray = rgb.convert("L")
    px = gray.load()

    def _is_uniform_bg_row(y: int) -> bool:
        step = max(1, w // 64)
        vals = [px[x, y] for x in range(0, w, step)]
        return all(v >= 245 for v in vals) or all(v <= 15 for v in vals)

    def _is_uniform_bg_col(x: int, y0: int, y1: int) -> bool:
        step = max(1, (y1 - y0) // 64)
        vals = [px[x, y] for y in range(y0, y1, step)]
        return all(v >= 245 for v in vals) or all(v <= 15 for v in vals)

    top = 0
    while top < h - 1 and _is_uniform_bg_row(top):
        top += 1
    bottom = h
    while bottom > top + 1 and _is_uniform_bg_row(bottom - 1):
        bottom -= 1
    if bottom - top < 10:
        return None
    left = 0
    while left < w - 1 and _is_uniform_bg_col(left, top, bottom):
        left += 1
    right = w
    while right > left + 1 and _is_uniform_bg_col(right - 1, top, bottom):
        right -= 1
    if right - left < 10:
        return None
    return (left, top, right, bottom)


def normalize_reader_pages_to_cover_size(
    pdf_blobs: list[bytes],
    is_reflowable: bool | None = None,
) -> list[bytes]:
    """Normalize all reader pages to one uniform book size, trim shared outer letterbox bars,
    and add a comfortable top/bottom reading margin on reflowable white-background text books."""
    if len(pdf_blobs) < 2:
        return pdf_blobs
    try:
        import pymupdf

        rects: list[tuple[float, float] | None] = []
        max_w = 0.0
        max_h = 0.0
        for blob in pdf_blobs:
            with pymupdf.open(stream=blob, filetype="pdf") as d:
                if len(d) != 1:
                    rects.append(None)
                    continue
                r = d[0].rect
                rects.append((r.width, r.height))
                if r.width >= 100 and r.height >= 100:
                    max_w = max(max_w, r.width)
                    max_h = max(max_h, r.height)
        if max_w < 100 or max_h < 100:
            return pdf_blobs

        # 1. Sample up to 24 evenly spaced single-page blobs to compute the book-wide non-white union bbox
        #    in uniform (max_w x max_h) centered coordinates.
        single_indices = [i for i, sz in enumerate(rects) if sz is not None]
        if not single_indices:
            return pdf_blobs
        sample_count = min(24, len(single_indices))
        sample_indices = sorted(
            {single_indices[0]}
            | {single_indices[int(k * (len(single_indices) - 1) / max(1, sample_count - 1))] for k in range(sample_count)}
        )

        def _non_white_bbox_in_canvas(blob: bytes, rw: float, rh: float) -> tuple[tuple[float, float, float, float], float, bool, bool] | None:
            with pymupdf.open(stream=blob, filetype="pdf") as d:
                pix = d[0].get_pixmap(dpi=72)
                if pix.width < 10 or pix.height < 10:
                    return None
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                bb = _content_bbox_stripping_letterbox(im)
                if not bb:
                    return None
                # Check if outer edges were dark/black letterbox bars and whether the page looks like
                # a reflowable white-paper text page (>=75% pure white background at native 72dpi without
                # downscale-blurring thin kanji strokes, and <=10% saturated color).
                gray = im.convert("L")
                top_val = gray.getpixel((pix.width // 2, 0))
                bot_val = gray.getpixel((pix.width // 2, pix.height - 1))
                has_dark_bar = (top_val <= 15 or bot_val <= 15) and (bb[1] > 2 or bb[3] < pix.height - 2)
                step_x = max(1, pix.width // 64)
                step_y = max(1, pix.height // 96)
                g_px = gray.load()
                rgb_px = im.load()
                total_samples = 0
                white_samples = 0
                color_samples = 0
                for sy_i in range(0, pix.height, step_y):
                    for sx_i in range(0, pix.width, step_x):
                        total_samples += 1
                        if g_px[sx_i, sy_i] >= 240:
                            white_samples += 1
                        r, g, b = rgb_px[sx_i, sy_i]
                        if max(r, g, b) - min(r, g, b) > 25:
                            color_samples += 1
                pure_white_ratio = white_samples / max(1, total_samples)
                colored_ratio = color_samples / max(1, total_samples)
                is_text_paper = (not has_dark_bar) and (pure_white_ratio >= 0.72) and (colored_ratio <= 0.10)
                crop_g = gray.crop(bb).resize((48, 48))
                non_white = sum(1 for v in crop_g.getdata() if v < 240)
                density = non_white / (48.0 * 48.0)
                sx = rw / pix.width
                sy = rh / pix.height
                ox = (max_w - rw) / 2.0
                oy = (max_h - rh) / 2.0
                return (ox + bb[0] * sx, oy + bb[1] * sy, ox + bb[2] * sx, oy + bb[3] * sy), density, has_dark_bar, is_text_paper

        cover_bbox: tuple[float, float, float, float] | None = None
        cover_density: float = 0.0
        has_any_dark_bar = False
        sample_bboxes: list[tuple[float, float, float, float]] = []
        body_bboxes: list[tuple[float, float, float, float]] = []
        body_white_edge_count = 0
        body_sample_total = 0
        for idx in sample_indices:
            sz = rects[idx]
            if sz is None:
                continue
            res = _non_white_bbox_in_canvas(pdf_blobs[idx], sz[0], sz[1])
            if res is None:
                continue
            bb, density, is_dark, is_white_edge = res
            if is_dark:
                has_any_dark_bar = True
            if idx != single_indices[0]:
                body_sample_total += 1
                if is_white_edge:
                    body_white_edge_count += 1
            if (bb[2] - bb[0]) >= max_w * 0.15 and (bb[3] - bb[1]) >= max_h * 0.15:
                sample_bboxes.append(bb)
                if idx == single_indices[0]:
                    cover_bbox = bb
                    cover_density = density
                else:
                    body_bboxes.append(bb)

        is_reflowable_text_book = (
            is_reflowable is True
            and not has_any_dark_bar
            and body_sample_total > 0
            and (body_white_edge_count / body_sample_total) >= 0.70
        )

        # Compute uniform book crop rect:
        # - For fixed-layout books (whether black- or white-letterboxed in Kindle's viewport),
        #   trim outer letterbox bars cleanly to 0pt so zero white/black bars remain.
        # - For confirmed reflowable white-paper text books, keep a clean 3% reading margin.
        crop_x0, crop_y0, crop_x1, crop_y1 = 0.0, 0.0, max_w, max_h
        if sample_bboxes:
            ux0 = min(b[0] for b in sample_bboxes)
            uy0 = min(b[1] for b in sample_bboxes)
            ux1 = max(b[2] for b in sample_bboxes)
            uy1 = max(b[3] for b in sample_bboxes)
            uw = ux1 - ux0
            uh = uy1 - uy0
            if uw >= max_w * 0.35 and uh >= max_h * 0.35:
                if is_reflowable_text_book:
                    pad_x = max(12.0, uw * 0.03)
                    pad_y = max(14.0, uh * 0.03)
                    threshold = 0.94
                else:
                    pad_x, pad_y = 0.0, 0.0
                    threshold = 0.99
                cx0 = max(0.0, ux0 - pad_x)
                cy0 = max(0.0, uy0 - pad_y)
                cx1 = min(max_w, ux1 + pad_x)
                cy1 = min(max_h, uy1 + pad_y)
                if (cx1 - cx0) < max_w * threshold or (cy1 - cy0) < max_h * threshold:
                    crop_x0, crop_y0, crop_x1, crop_y1 = cx0, cy0, cx1, cy1

        base_w = crop_x1 - crop_x0
        base_h = crop_y1 - crop_y0
        # For confirmed reflowable white-paper text books where body text sits flush against top/bottom edges,
        # extend the white canvas vertically to guarantee a comfortable ~5.5% top & bottom reading margin
        # (EBPAJ vertical-Japanese ebook guideline recommends 5-10% vertical breathing room).
        # Never add extra margins to fixed-layout books (magazines, comics, scores, print-replica PDFs).
        extra_top = 0.0
        extra_bot = 0.0
        extra_left = 0.0
        if is_reflowable_text_book and body_bboxes:
            body_top_spaces = sorted(max(0.0, b[1] - crop_y0) for b in body_bboxes)
            body_bot_spaces = sorted(max(0.0, crop_y1 - b[3]) for b in body_bboxes)
            p25_top = body_top_spaces[len(body_top_spaces) // 4]
            p25_bot = body_bot_spaces[len(body_bot_spaces) // 4]
            target_margin_y = round(base_h * 0.055, 2)
            extra_top = max(0.0, round(target_margin_y - p25_top, 2))
            extra_bot = max(0.0, round(target_margin_y - p25_bot, 2))
            if extra_top < 2.0:
                extra_top = 0.0
            if extra_bot < 2.0:
                extra_bot = 0.0

        out_h = round(base_h + extra_top + extra_bot, 2)
        out_w = round(base_w, 2)
        if is_reflowable_text_book:
            # Preserve the Cover's natural book aspect ratio (e.g. ~1:1.47) by adding symmetric
            # horizontal white paper margin so vertical reading margins never make body pages skinny.
            target_aspect = base_h / max(1.0, base_w)
            if cover_bbox is not None and cover_density >= 0.25:
                cw = cover_bbox[2] - cover_bbox[0]
                ch = cover_bbox[3] - cover_bbox[1]
                if cw >= max_w * 0.45 and ch >= max_h * 0.35:
                    target_aspect = min(2.1, max(0.65, ch / max(1.0, cw)))
            matched_w = round(out_h / max(0.5, target_aspect), 2)
            if matched_w > out_w + 1.0:
                extra_left = round((matched_w - out_w) / 2.0, 2)
                out_w = matched_w

        trimmed = (
            crop_x0 > 0.5
            or crop_y0 > 0.5
            or extra_top > 0.5
            or extra_bot > 0.5
            or extra_left > 0.5
            or abs(out_w - max_w) > 1.0
            or abs(out_h - max_h) > 1.0
        )

        normalized: list[bytes] = []
        for idx, (blob, sz) in enumerate(zip(pdf_blobs, rects)):
            if sz is None:
                normalized.append(blob)
                continue
            rw, rh = sz
            is_cover = (idx == single_indices[0])
            # On reflowable text books, crop illustrated cover page(s) cleanly to their artwork (0px white bars)
            # at the book's uniform width out_w instead of letterboxing them inside the taller text-page height.
            crop_cover_clean = False
            cover_page_bbox: tuple[float, float, float, float] | None = None
            if is_reflowable_text_book and idx <= single_indices[0] + 1:
                cov_info = _non_white_bbox_in_canvas(blob, rw, rh)
                if cov_info is not None:
                    cbb, cdens, _, c_is_text = cov_info
                    if not c_is_text and cdens >= 0.25:
                        cw = cbb[2] - cbb[0]
                        ch = cbb[3] - cbb[1]
                        if cw >= max_w * 0.45 and ch >= max_h * 0.35:
                            crop_cover_clean = True
                            cover_page_bbox = cbb

            # Only aspect-fit Page 0 when it is a genuine illustrated cover (density >= 0.25),
            # never when Page 0 is a sparse text page (e.g. 3-line disclaimer).
            fit_cover = False
            if not crop_cover_clean and is_cover and cover_bbox is not None and cover_density >= 0.25:
                cw = cover_bbox[2] - cover_bbox[0]
                ch = cover_bbox[3] - cover_bbox[1]
                if cw >= max_w * 0.45 and ch >= max_h * 0.35 and (cw < out_w * 0.86 or ch < out_h * 0.86):
                    fit_cover = True

            if not trimmed and not fit_cover and not crop_cover_clean and abs(rw - out_w) <= 1.0 and abs(rh - out_h) <= 1.0:
                normalized.append(blob)
                continue

            with pymupdf.open(stream=blob, filetype="pdf") as src_doc:
                out_doc = pymupdf.open()
                if crop_cover_clean and cover_page_bbox is not None:
                    ox = (max_w - rw) / 2.0
                    oy = (max_h - rh) / 2.0
                    src_clip = pymupdf.Rect(
                        max(0.0, cover_page_bbox[0] - ox),
                        max(0.0, cover_page_bbox[1] - oy),
                        min(rw, cover_page_bbox[2] - ox),
                        min(rh, cover_page_bbox[3] - oy),
                    )
                    scale = out_w / max(1.0, src_clip.width)
                    cov_h = round(src_clip.height * scale, 2)
                    page = out_doc.new_page(width=out_w, height=cov_h)
                    page.show_pdf_page(
                        pymupdf.Rect(0.0, 0.0, out_w, cov_h),
                        src_doc,
                        0,
                        clip=src_clip,
                    )
                elif fit_cover and cover_bbox is not None:
                    page = out_doc.new_page(width=out_w, height=out_h)
                    ox = (max_w - rw) / 2.0
                    oy = (max_h - rh) / 2.0
                    src_clip = pymupdf.Rect(
                        max(0.0, cover_bbox[0] - ox),
                        max(0.0, cover_bbox[1] - oy),
                        min(rw, cover_bbox[2] - ox),
                        min(rh, cover_bbox[3] - oy),
                    )
                    scale = min(out_w / max(1.0, src_clip.width), out_h / max(1.0, src_clip.height))
                    dw = src_clip.width * scale
                    dh = src_clip.height * scale
                    dx = (out_w - dw) / 2.0
                    dy = (out_h - dh) / 2.0
                    page.show_pdf_page(
                        pymupdf.Rect(dx, dy, dx + dw, dy + dh),
                        src_doc,
                        0,
                        clip=src_clip,
                    )
                else:
                    page = out_doc.new_page(width=out_w, height=out_h)
                    ox = (max_w - rw) / 2.0
                    oy = (max_h - rh) / 2.0
                    # Map uniform canvas crop_rect back to source page coordinates.
                    src_x0 = max(0.0, crop_x0 - ox)
                    src_y0 = max(0.0, crop_y0 - oy)
                    src_x1 = min(rw, crop_x1 - ox)
                    src_y1 = min(rh, crop_y1 - oy)
                    dest_x0 = (ox + src_x0) - crop_x0 + extra_left
                    dest_y0 = (oy + src_y0) - crop_y0 + extra_top
                    dest_x1 = (ox + src_x1) - crop_x0 + extra_left
                    dest_y1 = (oy + src_y1) - crop_y0 + extra_top
                    page.show_pdf_page(
                        pymupdf.Rect(dest_x0, dest_y0, dest_x1, dest_y1),
                        src_doc,
                        0,
                        clip=pymupdf.Rect(src_x0, src_y0, src_x1, src_y1),
                    )
                normalized.append(out_doc.tobytes())
                out_doc.close()
        return normalized
    except Exception:
        return pdf_blobs


def merge_pdf_bytes_list(pdf_blobs: Iterable[bytes], output_path: Path) -> int:
    """Merge a sequence of PDF byte blobs into a single PDF file and return total page count."""
    writer = PdfWriter()
    total_pages = 0

    for idx, blob in enumerate(pdf_blobs, start=1):
        try:
            reader = PdfReader(io.BytesIO(blob))
        except Exception as exc:
            log(f"[WARN] Skipping unreadable PDF #{idx}: {exc}")
            continue

        for page in reader.pages:
            writer.add_page(page)
            total_pages += 1

    if total_pages == 0:
        raise RuntimeError("No valid PDF pages could be merged.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        writer.write(handle)

    return total_pages


def merge_images_to_pdf(image_paths: list[Path], output_path: Path) -> int:
    """Merge PNG screenshots into a single PDF file and return page count."""
    if not image_paths:
        raise RuntimeError("No captured page images to merge.")

    images: list[Image.Image] = []
    try:
        for img_path in image_paths:
            with Image.open(img_path) as opened:
                images.append(opened.convert("RGB"))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        images[0].save(
            output_path,
            "PDF",
            save_all=True,
            append_images=images[1:],
        )
        return len(images)
    finally:
        for img in images:
            img.close()


def crawl_html_for_pdfs(
    session: requests.Session,
    start_url: str,
    max_pages: int,
    timeout: int,
) -> list[str]:
    """Crawl paginated HTML pages starting at start_url and collect PDF URLs in order."""
    visited_pages: set[str] = set()
    collected_pdfs: list[str] = []
    seen_pdfs: set[str] = set()

    current_url: str | None = start_url
    page_num = 0

    while current_url and current_url not in visited_pages and page_num < max_pages:
        page_num += 1
        visited_pages.add(current_url)
        log(f"[Crawl] Page {page_num}: {current_url}")

        try:
            resp = session.get(current_url, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log(f"[WARN] Failed to load page {current_url}: {exc}")
            break

        content_type = resp.headers.get("Content-Type", "").lower()
        if "application/pdf" in content_type or is_pdf_bytes(resp.content):
            if resp.url not in seen_pdfs:
                seen_pdfs.add(resp.url)
                collected_pdfs.append(resp.url)
            break

        if "html" not in content_type and b"<html" not in resp.content[:500].lower():
            break

        pdf_links = extract_pdf_links_from_html(resp.content, resp.url)
        for link in pdf_links:
            if link not in seen_pdfs:
                seen_pdfs.add(link)
                collected_pdfs.append(link)

        log(f"  Found {len(pdf_links)} PDF link(s) on page {page_num} (total unique: {len(collected_pdfs)})")
        next_url = find_next_page_url(resp.content, resp.url)
        current_url = next_url

    return collected_pdfs


def resolve_cdp_url(candidates: tuple[str, ...] = (
    "http://127.0.0.1:9223",
    "http://[::1]:9223",
    "http://127.0.0.1:9222",
    "http://[::1]:9222",
)) -> str | None:
    """Return the first reachable Chromium CDP endpoint URL, or None."""
    for candidate in candidates:
        try:
            resp = requests.get(f"{candidate}/json/version", timeout=1.5)
            if resp.status_code == 200 and "webSocketDebuggerUrl" in resp.text:
                return candidate
        except requests.RequestException:
            continue
    return None



PRINT_CLEANUP_STYLE_ID = "__webtpdf_print_cleanup__"


def inject_print_cleanup(page) -> dict:
    """Temporarily normalize page layout for clean multi-page or fixed-layout (.pf) PDF printing."""
    try:
        result = page.evaluate(
            """(styleId) => {
                if (document.getElementById(styleId)) return { fixedLayout: false };

                const pf = document.querySelector('.pf');
                if (pf) {
                    const st = window.getComputedStyle(pf);
                    const w = Math.ceil(parseFloat(st.width) || pf.offsetWidth);
                    const h = Math.ceil(parseFloat(st.height) || pf.offsetHeight);
                    if (w > 100 && h > 100) {
                        const style = document.createElement('style');
                        style.id = styleId;
                        style.textContent = `
                            @page { size: ${w}px ${h + 2}px !important; margin: 0 !important; }
                            @media print {
                                html, body {
                                    width: ${w}px !important; height: auto !important; min-height: 0 !important;
                                    margin: 0 !important; padding: 0 !important;
                                    background: #fff !important; overflow: visible !important;
                                }
                                header, nav, footer, [data-testid="alternativeFormatCallout"], [class*="alternativeFormatCallout" i] { display: none !important; }
                                #sbo-rt-content, #pdf-main, .pdf-page {
                                    margin: 0 !important; padding: 0 !important;
                                    transform: none !important; width: ${w}px !important;
                                    max-width: ${w}px !important; min-width: 0 !important;
                                    height: auto !important; min-height: 0 !important;
                                    position: static !important; left: 0 !important; top: 0 !important;
                                }
                                .pdf-page {
                                    height: ${h}px !important; overflow: hidden !important;
                                    break-after: page !important; page-break-after: always !important;
                                    break-inside: avoid !important; page-break-inside: avoid !important;
                                }
                                .pdf-page:last-of-type {
                                    break-after: auto !important; page-break-after: auto !important;
                                }
                                .pf {
                                    margin: 0 !important; border: none !important; box-shadow: none !important;
                                }
                            }
                        `;
                        document.head.appendChild(style);

                        const rt = document.querySelector('#sbo-rt-content') || document.querySelector('#pdf-main') || pf.parentElement;
                        let cur = rt;
                        while (cur && cur.parentElement && cur.parentElement !== document.body) {
                            for (const sib of cur.parentElement.children) {
                                if (sib !== cur && sib.tagName !== 'STYLE' && sib.tagName !== 'SCRIPT') {
                                    sib.setAttribute('data-ec-hide', sib.style.display || '');
                                    sib.style.setProperty('display', 'none', 'important');
                                }
                            }
                            cur.parentElement.setAttribute('data-ec-pad', cur.parentElement.style.padding || '');
                            cur.parentElement.style.setProperty('display', 'block', 'important');
                            cur.parentElement.style.setProperty('padding', '0', 'important');
                            cur.parentElement.style.setProperty('margin', '0', 'important');
                            cur.parentElement.style.setProperty('height', 'auto', 'important');
                            cur.parentElement.style.setProperty('min-height', '0', 'important');
                            cur = cur.parentElement;
                        }
                        return { fixedLayout: true, pfCount: document.querySelectorAll('.pf').length };
                    }
                }

                const style = document.createElement('style');
                style.id = styleId;
                style.textContent = `
                    @page {
                        margin: 0.5in 0.4in !important;
                    }
                    @media print {
                        html, body, main, article, section, [role="main"] {
                            overflow: visible !important;
                            height: auto !important;
                            max-height: none !important;
                            width: 100% !important;
                            max-width: none !important;
                            margin: 0 !important;
                            padding: 0 !important;
                        }
                        header, nav, footer, aside,
                        [role="complementary"], [role="navigation"], [role="banner"],
                        [data-testid="alternativeFormatCallout"],
                        [class*="alternativeFormatCallout" i],
                        [class*="other-formats" i] {
                            display: none !important;
                        }
                        p, li, h1, h2, h3, h4, h5, h6, pre, blockquote, tr, figure, img {
                            break-inside: avoid;
                            page-break-inside: avoid;
                            orphans: 3;
                            widows: 3;
                        }
                    }
                `;
                document.head.appendChild(style);

                const rt = document.querySelector('#sbo-rt-content, .sbo-reader-content, [data-testid="book-content"], [class*="_contentContainer_"], main article, main');
                if (rt && ((rt.innerText || '').trim().length > 0 || rt.querySelector('img, canvas, svg'))) {
                    let cur = rt;
                    while (cur && cur.parentElement && cur.parentElement !== document.body) {
                        for (const sib of cur.parentElement.children) {
                            if (sib !== cur && sib.tagName !== 'STYLE' && sib.tagName !== 'SCRIPT') {
                                sib.setAttribute('data-ec-hide', sib.style.display || '');
                                sib.style.setProperty('display', 'none', 'important');
                            }
                        }
                        cur.parentElement.setAttribute('data-ec-style', cur.parentElement.getAttribute('style') || '');
                        cur.parentElement.style.setProperty('display', 'block', 'important');
                        cur.parentElement.style.setProperty('width', '100%', 'important');
                        cur.parentElement.style.setProperty('max-width', 'none', 'important');
                        cur.parentElement.style.setProperty('min-width', '0', 'important');
                        cur.parentElement.style.setProperty('padding', '0', 'important');
                        cur.parentElement.style.setProperty('margin', '0', 'important');
                        cur = cur.parentElement;
                    }
                    rt.setAttribute('data-ec-style', rt.getAttribute('style') || '');
                    rt.style.setProperty('display', 'block', 'important');
                    rt.style.setProperty('width', '100%', 'important');
                    rt.style.setProperty('max-width', 'none', 'important');
                    rt.style.setProperty('min-width', '0', 'important');
                    rt.style.setProperty('margin', '0 auto', 'important');
                }

                for (const el of document.querySelectorAll('*')) {
                    const pos = window.getComputedStyle(el).position;
                    if (pos === 'fixed' || pos === 'sticky') {
                        el.setAttribute('data-ec-orig-pos', el.style.position || '');
                        el.style.setProperty('position', 'static', 'important');
                    }
                }
                return { fixedLayout: false };
            }""",
            PRINT_CLEANUP_STYLE_ID,
        )
        return result if isinstance(result, dict) else {"fixedLayout": False}
    except Exception:
        return {"fixedLayout": False}


def remove_print_cleanup(page) -> None:
    """Restore DOM styles modified by inject_print_cleanup."""
    try:
        page.evaluate(
            """(styleId) => {
                document.getElementById(styleId)?.remove();
                for (const el of document.querySelectorAll('[data-ec-hide]')) {
                    el.style.display = el.getAttribute('data-ec-hide');
                    el.removeAttribute('data-ec-hide');
                }
                for (const el of document.querySelectorAll('[data-ec-pad]')) {
                    el.style.padding = el.getAttribute('data-ec-pad');
                    el.style.removeProperty('display');
                    el.style.removeProperty('margin');
                    el.style.removeProperty('height');
                    el.style.removeProperty('min-height');
                    el.removeAttribute('data-ec-pad');
                }
                for (const el of document.querySelectorAll('[data-ec-style]')) {
                    const origStyle = el.getAttribute('data-ec-style');
                    if (origStyle) {
                        el.setAttribute('style', origStyle);
                    } else {
                        el.removeAttribute('style');
                    }
                    el.removeAttribute('data-ec-style');
                }
                for (const el of document.querySelectorAll('[data-ec-orig-pos]')) {
                    const orig = el.getAttribute('data-ec-orig-pos');
                    if (orig) {
                        el.style.position = orig;
                    } else {
                        el.style.removeProperty('position');
                    }
                    el.removeAttribute('data-ec-orig-pos');
                }
            }""",
            PRINT_CLEANUP_STYLE_ID,
        )
    except Exception:
        pass


def pdf_content_signature(pdf_bytes: bytes) -> str:
    """Return a deterministic content signature for a PDF ignoring metadata timestamps."""
    try:
        import fitz

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            parts: list[str] = []
            for pg in doc:
                img_hashes: list[str] = []
                for img in pg.get_images():
                    xref = img[0]
                    try:
                        img_bytes = doc.extract_image(xref)["image"]
                        img_hashes.append(hashlib.sha256(img_bytes).hexdigest()[:16])
                    except Exception:
                        img_hashes.append(str(xref))
                parts.append(
                    f"{pg.rect.width:.1f}x{pg.rect.height:.1f}|{','.join(img_hashes)}|{pg.get_text()}"
                )
            return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    except Exception:
        return hashlib.sha256(pdf_bytes).hexdigest()


def strip_trailing_blank_pages(pdf_bytes: bytes) -> bytes:
    """Remove trailing completely blank pages produced by CSS page-break-after."""
    try:
        import fitz

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if len(doc) <= 1:
                return pdf_bytes
            last_non_blank = len(doc) - 1
            while last_non_blank > 0:
                pg = doc[last_non_blank]
                if pg.get_text().strip() or pg.get_images():
                    break
                last_non_blank -= 1
            if last_non_blank < len(doc) - 1:
                doc.delete_pages(from_page=last_non_blank + 1, to_page=len(doc) - 1)
                return doc.tobytes()
    except Exception:
        pass
    return pdf_bytes


def is_pdf_visually_blank(pdf_bytes: bytes) -> bool:
    """Return True if a captured PDF has zero text/images or renders completely white."""
    try:
        import fitz

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if len(doc) == 0:
                return True
            for pg in doc:
                if pg.get_text().strip():
                    return False
                if pg.get_images():
                    pix = pg.get_pixmap(dpi=36, alpha=False)
                    if pix.samples and min(pix.samples) < 250:
                        return False
                else:
                    pix = pg.get_pixmap(dpi=36, alpha=False)
                    if pix.samples and min(pix.samples) < 250:
                        return False
            return True
    except Exception:
        return False


def is_pdf_loading_spinner(pdf_bytes: bytes) -> bool:
    """Return True if a captured reader PDF page is a dark-mode or light-mode loading spinner screen
    (e.g. Kindle Manga Web Reader's all-black screen with a tiny centered rotating arc)."""
    try:
        import pymupdf

        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            if len(doc) != 1:
                return False
            pg = doc[0]
            if pg.get_text().strip():
                return False
            pix = pg.get_pixmap(dpi=36, alpha=False)
            if pix.width < 10 or pix.height < 10:
                return False
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
            w, h = im.size
            px = im.load()
            total = w * h
            dark_count = 0
            white_count = 0
            non_bg_outside_center = 0
            cx0, cx1 = int(w * 0.30), int(w * 0.70)
            cy0, cy1 = int(h * 0.30), int(h * 0.70)
            for y in range(h):
                for x in range(w):
                    v = px[x, y]
                    if v <= 20:
                        dark_count += 1
                    elif v >= 240:
                        white_count += 1
            dark_ratio = dark_count / total
            white_ratio = white_count / total
            # Dark-mode loading spinner: >= 97.5% pure black with at most a tiny spinner in the center.
            if dark_ratio >= 0.975:
                for y in range(h):
                    for x in range(w):
                        if px[x, y] > 20 and not (cx0 <= x <= cx1 and cy0 <= y <= cy1):
                            non_bg_outside_center += 1
                if (non_bg_outside_center / total) < 0.005:
                    return True
            # Light-mode loading spinner: >= 97.5% pure white with non-white pixels strictly inside center box.
            if white_ratio >= 0.975 and dark_count < total * 0.025:
                non_white_in_center = 0
                for y in range(h):
                    for x in range(w):
                        if px[x, y] < 240:
                            if cx0 <= x <= cx1 and cy0 <= y <= cy1:
                                non_white_in_center += 1
                            else:
                                non_bg_outside_center += 1
                if non_white_in_center > 0 and (non_bg_outside_center / total) < 0.003:
                    return True
            return False
    except Exception:
        return False


def _extract_reader_asin(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:[?&]asin=|/manga/)([A-Z0-9]{10})", url, re.I)
    return m.group(1).upper() if m else None


READER_UPSELL_MARKERS: tuple[str, ...] = (
    "サンプル版は以上です",
    "続きは本編をご覧ください",
    "シリーズの次の",
    "1-click で今すぐ購入",
    "1-clickで今すぐ購入",
    "注文を確定する",
    "before you go",
    "rate this book",
    "この本を評価",
    "end of sample",
    "next in series",
    "次巻のサンプル",
    "サンプル版の終わり",
)


def is_reader_upsell_or_next_book(
    page,
    start_asin: str | None,
    start_title: str | None,
) -> str | None:
    """Return a stop reason if Kindle Reader has transitioned past the end of the book into
    a store upsell screen, rating overlay, or the free sample of the next volume in a series."""
    try:
        cur_asin = _extract_reader_asin(page.url)
        if start_asin and cur_asin and cur_asin != start_asin:
            return f"Book ASIN changed ({start_asin} -> {cur_asin}; next volume/sample detected)"
    except Exception:
        pass
    try:
        body_text = (page.inner_text("body", timeout=1500) or "")
        low = body_text.lower()
        for marker in READER_UPSELL_MARKERS:
            if marker in low:
                return f"End-of-book / store upsell screen detected ('{marker}')"
        if "続けて読む" in body_text and ("kindle 価格" in body_text or "kindle価格" in body_text or "購入" in body_text):
            return "Next-volume purchase upsell detected ('続けて読む')"
    except Exception:
        pass
    if start_title:
        try:
            cur_title, _ = extract_book_title_and_date(page)
            if cur_title:
                s_norm = re.sub(r"\s+", "", start_title).lower()
                c_norm = re.sub(r"\s+", "", cur_title).lower()
                # Ignore generic library/Kindle chrome titles; trigger only when a genuinely different book title appears.
                if (
                    len(s_norm) >= 3
                    and len(c_norm) >= 3
                    and c_norm not in ("kindle", "ライブラリ", "kindlelibrary")
                    and s_norm not in c_norm
                    and c_norm not in s_norm
                ):
                    return f"Book title changed ('{start_title}' -> '{cur_title}'; next volume detected)"
        except Exception:
            pass
    return None


LOADING_OR_ERROR_PHRASES: tuple[str, ...] = (
    "hang tight",
    "this'll take just a sec",
    "this\u2019ll take just a sec",
    "oops... something went wrong",
)


def page_has_loading_or_error_text(text: str) -> bool:
    """Return True if a page text contains a known loading placeholder or reader error screen."""
    low = (text or "").lower()
    if any(ph in low for ph in LOADING_OR_ERROR_PHRASES):
        return True
    if "something went wrong" in low and "back to library" in low:
        return True
    return False


def is_pdf_loading_or_error(pdf_bytes: bytes) -> bool:
    """Return True if any page in the captured PDF bytes contains loading or error screen text."""
    try:
        import fitz

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for pg in doc:
                if page_has_loading_or_error_text(pg.get_text() or ""):
                    return True
    except Exception:
        pass
    return False


def capture_fixed_layout_pf_pages(page, cdp_session=None) -> bytes | None:
    """Capture each .pf fixed-layout page via element screenshot so scaled/absolute layouts never clip."""
    pfs = page.locator(".pf")
    pf_count = pfs.count()
    if not pf_count:
        return None


    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('header, nav, [class*="iconMenu"], [class*="bottomIcons"]').forEach(el => {
                    el.setAttribute('data-ec-pf-hide', el.style.display || '');
                    el.style.setProperty('display', 'none', 'important');
                });
            }"""
        )
        images: list[Image.Image] = []
        try:
            for idx in range(pf_count):
                el = pfs.nth(idx)
                el.scroll_into_view_if_needed(timeout=5000)
                png_bytes = el.screenshot(timeout=15000)
                with Image.open(io.BytesIO(png_bytes)) as opened:
                    images.append(opened.convert("RGB"))

            if not images:
                return None


            buf = io.BytesIO()
            images[0].save(
                buf,
                "PDF",
                save_all=True,
                append_images=images[1:],
            )
            return buf.getvalue()
        finally:
            for img in images:
                img.close()
    finally:
        try:
            page.evaluate(
                """() => {
                    document.querySelectorAll('[data-ec-pf-hide]').forEach(el => {
                        el.style.display = el.getAttribute('data-ec-pf-hide');
                        el.removeAttribute('data-ec-pf-hide');
                    });
                }"""
            )
        except Exception:
            pass


def is_paginated_reader(page) -> bool:
    """Return True if the page is paginated ebook reader."""
    try:
        if "read.amazon." in (page.url or "").lower():
            return True
        return page.locator("img[src^='blob:https://read.amazon.']").count() > 0
    except Exception:
        return False


def dismiss_reader_dialogs(page) -> bool:
    """Dismiss paginated ebook reader modal prompts (e.g. 'Most Recent Page Read') if visible.

    Returns True if a 'Most Recent Page Read' prompt was dismissed.
    """
    try:
        body_text = (page.inner_text("body", timeout=2000) or "").lower()
        is_recent_prompt = (
            "most recent page read" in body_text
            or "most recent page" in body_text
            or "最後に読んだページ" in body_text
        )
        no_pattern = re.compile(r"^(?:No|いいえ)$", re.I)
        candidates = [
            page.get_by_role("button", name=no_pattern).filter(visible=True).first,
            page.locator("button:has-text('No'), button:has-text('いいえ'), ion-button:has-text('No'), ion-button:has-text('いいえ'), [role='button']:has-text('No'), [role='button']:has-text('いいえ')").filter(visible=True).first,
        ]
        if is_recent_prompt:
            candidates.append(page.get_by_text(no_pattern, exact=True).filter(visible=True).first)
        for no_btn in candidates:
            try:
                if no_btn.count() > 0 and no_btn.is_visible():
                    no_btn.click(timeout=1500)
                    page.wait_for_timeout(400)
                    return is_recent_prompt
            except Exception:
                continue
    except Exception:
        pass
    return False


def is_reader_not_at_start(page) -> bool:
    """Return True if paginated ebook reader is currently past page/location 1 or > 0%."""
    try:
        marker = get_reader_position(page)
        if marker is not None and marker[1] > 1:
            return True
        cur = get_reader_page_number(page)
        if cur is not None and cur > 1:
            return True
        pct = get_reader_progress_percent(page)
        if pct is not None and pct > 0:
            return True
        body_text = page.inner_text("body", timeout=2000) or ""
        m_pct = re.search(r"[●•]\s*([1-9]\d*)%", body_text)
        if m_pct:
            return True
    except Exception:
        pass
    return False


def jump_to_reader_toc_start(page) -> tuple[bool, bool]:
    """Open the reader's TOC and jump to Cover / 表紙 or the very first TOC entry.

    Returns (jumped, clicked_explicit_cover).
    """
    toggle = page.get_by_role("button", name=READER_TOC_NAME).or_(
        page.locator("#kr-toc-button, #reader-toc-button, [data-testid='toc-button']")
    )
    clicked_button = None
    for button in toggle.all():
        if button.is_visible():
            button.click(timeout=2000)
            clicked_button = button
            break
    if clicked_button is None:
        return False, False

    heading = page.get_by_role("heading", name=READER_TOC_NAME).filter(visible=True).first
    if not heading.count():
        heading = page.get_by_text(READER_TOC_NAME, exact=True).filter(visible=True).last
    try:
        heading.wait_for(state="visible", timeout=2500)
        # First, directly match an explicit Cover / 表紙 entry in the open TOC sidebar.
        cover_pattern = re.compile(r"^(?:表紙|カバー|書影|Front\s*Cover|Book\s*Cover|Cover|Beginning|先頭)$", re.I)
        cover_loc = page.get_by_text(cover_pattern).filter(visible=True).first
        if cover_loc.count() > 0:
            cover_loc.click(timeout=2500)
            page.wait_for_timeout(600)
            if heading.is_visible():
                try:
                    clicked_button.click(timeout=2000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass
            if heading.is_visible():
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                except Exception:
                    pass
            _wait_for_reader_reader(page, timeout_ms=8000)
            page.wait_for_timeout(500)
            return True, True

        panel = heading.locator("xpath=..")
        entry_selector = "button, a, [role=option], [role=menuitem], [role=button], li[tabindex]"
        for _ in range(4):
            if panel.evaluate("node => node === document.body || node === document.documentElement"):
                break
            info = panel.evaluate(r"""(panel, {selector}) => {
                const entries = [...panel.querySelectorAll(selector)];
                const chapters = entries.filter(node =>
                    node.getClientRects().length && node.textContent.trim() &&
                    !node.querySelector(selector) && !node.disabled &&
                    node.getAttribute('aria-disabled') !== 'true'
                );
                if (!chapters.length) return null;
                const cover = chapters.find(node => /^(?:cover|front\s*cover|book\s*cover|表紙|カバー|書影|beginning|先頭)$/i.test(node.textContent.trim()));
                const chosen = cover || chapters[0];
                return { index: entries.indexOf(chosen), isCover: Boolean(cover) };
            }""", {"selector": entry_selector})
            if info is not None and info.get("index", -1) >= 0:
                panel.locator(entry_selector).nth(info["index"]).click(timeout=3000)
                page.wait_for_timeout(600)
                # On wide viewports the TOC sidebar stays open after clicking an item; close it explicitly.
                if heading.is_visible():
                    try:
                        clicked_button.click(timeout=2000)
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
                if heading.is_visible():
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(300)
                    except Exception:
                        pass
                _wait_for_reader_reader(page, timeout_ms=8000)
                page.wait_for_timeout(500)
                return True, bool(info.get("isCover"))
            panel = panel.locator("xpath=..")
        return False, False
    except Exception:
        return False, False
    finally:
        try:
            if heading.is_visible() and clicked_button.is_visible():
                clicked_button.click(timeout=2000)
        except Exception:
            pass


def _detect_chevron_turn_key(page) -> str | None:
    """Inspect Kindle Cloud Reader chevron buttons to determine which key advances to the next page."""
    try:
        detected = page.evaluate("""() => {
            const left = document.querySelector('#kr-chevron-left, [data-testid="chevron-left"]');
            const right = document.querySelector('#kr-chevron-right, [data-testid="chevron-right"]');
            const isVisible = (el) => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            };
            const label = (el) => {
                if (!el) return '';
                return [
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.textContent || '',
                ].join(' ').toLowerCase();
            };
            const lVis = isVisible(left);
            const rVis = isVisible(right);
            const lText = label(left);
            const rText = label(right);
            const isNext = (s) => s.includes('next') || s.includes('次');
            const isPrev = (s) => s.includes('prev') || s.includes('前');
            if (isNext(lText) && !isNext(rText)) return 'ArrowLeft';
            if (isNext(rText) && !isNext(lText)) return 'ArrowRight';
            if (isPrev(rText) && !isPrev(lText)) return 'ArrowLeft';
            if (isPrev(lText) && !isPrev(rText)) return 'ArrowRight';
            // At the very first page (Cover), only the forward chevron is visible.
            if (lVis && !rVis) return 'ArrowLeft';
            if (rVis && !lVis) return 'ArrowRight';
            return null;
        }""")
        if detected in ("ArrowLeft", "ArrowRight"):
            return detected
    except Exception:
        pass
    return None


def detect_reader_turn_direction(page, current_turn_key: str) -> str:
    """Auto-detect whether ArrowLeft or ArrowRight advances pages; return corrected key."""
    dismiss_reader_dialogs(page)
    reverse_key = "ArrowRight" if current_turn_key == "ArrowLeft" else "ArrowLeft"

    # 1. Check DOM chevron aria-label / title first (instant and zero page movement).
    chevron_key = _detect_chevron_turn_key(page)
    if chevron_key in ("ArrowLeft", "ArrowRight"):
        if chevron_key != current_turn_key:
            log(f"[Reader] Auto-detected turn direction from reader controls: {current_turn_key} -> {chevron_key}")
        return chevron_key

    # 2. Live 1-step probe: press current_turn_key once and check whether position/view advances.
    try:
        pos0 = get_reader_position(page)
        pct0 = get_reader_progress_percent(page)
        src0 = page.evaluate(READER_PAGE_SOURCE)
        hash0 = None
        if not src0:
            f0 = _capture_clean_reader_frame(page, timeout_ms=5000)
            if f0 is not None:
                hash0 = hashlib.sha256(f0).hexdigest()

        page.keyboard.press(current_turn_key)
        moved = wait_for_reader_turn(page, src0, timeout_ms=1500) if src0 else False
        if not src0:
            page.wait_for_timeout(450)
        pos1 = get_reader_position(page)
        pct1 = get_reader_progress_percent(page)
        if not moved and hash0 is not None:
            f1 = _capture_clean_reader_frame(page, timeout_ms=5000)
            if f1 is not None and hashlib.sha256(f1).hexdigest() != hash0:
                moved = True
        if pos0 is not None and pos1 is not None and pos1 != pos0:
            moved = True

        if moved:
            # Step back to the starting page before returning; if reverse key cannot return to cover
            # (some Kindle books block stepping back onto cover via arrow keys), jump back via TOC/Location 1.
            def _restore_start_page(back_key: str) -> None:
                page.keyboard.press(back_key)
                page.wait_for_timeout(500)
                if hash0 is not None:
                    f_back = _capture_clean_reader_frame(page, timeout_ms=5000)
                    if f_back is not None and hashlib.sha256(f_back).hexdigest() != hash0:
                        _reveal_reader_toolbar(page)
                        _, got_cover = jump_to_reader_toc_start(page)
                        if not got_cover:
                            seek_reader_position(page, 1, target_is_printed_page=False)
                        page.wait_for_timeout(400)

            if pos0 is not None and pos1 is not None and pos1[1] < pos0[1]:
                _restore_start_page(reverse_key)
                log(f"[Reader] Auto-corrected turn direction (position decreased): {current_turn_key} -> {reverse_key}")
                return reverse_key
            if pct0 is not None and pct1 is not None and pct1 < pct0:
                _restore_start_page(reverse_key)
                log(f"[Reader] Auto-corrected turn direction (progress decreased): {current_turn_key} -> {reverse_key}")
                return reverse_key
            # Current key advanced forward — step back once so capture starts at page 1.
            _restore_start_page(reverse_key)
            return current_turn_key

        # Pressing current_turn_key did nothing (likely at page 1 with wrong direction); probe reverse_key.
        page.keyboard.press(reverse_key)
        rev_moved = wait_for_reader_turn(page, src0, timeout_ms=1500) if src0 else False
        if not src0:
            page.wait_for_timeout(450)
        pos2 = get_reader_position(page)
        if not rev_moved and hash0 is not None:
            f2 = _capture_clean_reader_frame(page, timeout_ms=5000)
            if f2 is not None and hashlib.sha256(f2).hexdigest() != hash0:
                rev_moved = True
        if pos0 is not None and pos2 is not None and pos2[1] > pos0[1]:
            rev_moved = True

        if rev_moved:
            # Step back once to return to page 1.
            page.keyboard.press(current_turn_key)
            page.wait_for_timeout(400)
            log(f"[Reader] Auto-corrected turn direction (start-page probe): {current_turn_key} -> {reverse_key}")
            return reverse_key
    except Exception:
        pass
    return current_turn_key


def _is_reader_toolbar_in_viewport(page) -> bool:
    """Return True if Kindle's top toolbar (TOC / Reader menu) is currently slid into the viewport (y >= 0)."""
    try:
        btn = page.get_by_role("button", name=READER_TOC_NAME).or_(
            page.locator("ion-button[aria-label='Reader menu'], #kr-toc-button, #reader-toc-button, [data-testid='top_menu_navigation_menu']")
        ).first
        if btn.count() > 0:
            box = btn.bounding_box(timeout=1000)
            if box is not None:
                return box.get("y", -1) >= 0
    except Exception:
        pass
    return False


def _reveal_reader_toolbar(page) -> None:
    """Click the center of the reader viewport only when the top toolbar is slid off-screen (y < 0)."""
    try:
        if _is_reader_toolbar_in_viewport(page):
            return
        vp = page.viewport_size
        cx = (vp["width"] / 2.0) if vp else 430.0
        cy = (vp["height"] / 2.0) if vp else 600.0
        page.mouse.click(cx, cy)
        page.wait_for_timeout(500)
    except Exception:
        pass


def rewind_reader_to_start(page, turn_key: str, max_steps: int = 600) -> None:
    """Jump to TOC Cover or Location/Page 1 when available, then step back any remaining pages to cover."""
    _reveal_reader_toolbar(page)
    jumped_by_nav = False
    clicked_explicit_cover = False
    try:
        toc_ok, clicked_explicit_cover = jump_to_reader_toc_start(page)
        page.wait_for_timeout(500)
        pos_after_toc = get_reader_position(page)
        pct_after_toc = get_reader_progress_percent(page)
        if toc_ok or (pos_after_toc is not None and pos_after_toc[1] <= 3) or (pct_after_toc is not None and pct_after_toc == 0):
            jumped_by_nav = True
            log(f"[Reader] Jumped near start via Table of Contents (cover={clicked_explicit_cover}, pos={pos_after_toc}, pct={pct_after_toc}).")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    if clicked_explicit_cover:
        # We explicitly clicked 表紙 / Cover in TOC — do NOT call seek_reader_position(1)
        # (which can jump forward to Page 1 disclaimer), and close toolbar so cover captures cleanly.
        try:
            if _is_reader_toolbar_in_viewport(page):
                vp = page.viewport_size
                cx = (vp["width"] / 2.0) if vp else 430.0
                cy = (vp["height"] / 2.0) if vp else 600.0
                page.mouse.click(cx, cy)
                page.wait_for_timeout(350)
        except Exception:
            pass
        return
    if not clicked_explicit_cover:
        # Even when TOC succeeded (e.g. TOC starts at "はじめに" / "目次" without a "表紙" entry),
        # ALWAYS try jumping directly to Location 1 first so books whose cover sits before Page 1
        # land straight on the cover before stepping backward.
        _reveal_reader_toolbar(page)
        for prefer_page in (False, True):
            try:
                if seek_reader_position(page, 1, target_is_printed_page=prefer_page):
                    page.wait_for_timeout(600)
                    jumped_by_nav = True
                    log(f"[Reader] Jumped near start via {'Page' if prefer_page else 'Location'} 1.")
                    break
            except Exception:
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
    # Even after TOC/Location 1 jump, always close any toolbar/menu and step backward until we truly hit the cover
    # (TOC often lands on page 2-4 like "Characters" or "Intro", which can still show 0%-1% or Page 1).
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if _is_reader_toolbar_in_viewport(page):
            vp = page.viewport_size
            cx = (vp["width"] / 2.0) if vp else 430.0
            cy = (vp["height"] / 2.0) if vp else 600.0
            page.mouse.click(cx, cy)
            page.wait_for_timeout(350)
    except Exception:
        pass
    reverse_key = "ArrowRight" if turn_key == "ArrowLeft" else "ArrowLeft"
    reverse_chevron = "#kr-chevron-right, [data-testid='chevron-right']" if reverse_key == "ArrowRight" else "#kr-chevron-left, [data-testid='chevron-left']"
    log(f"[Reader] Stepping back to true cover (page 1) via {reverse_key}...")
    unchanged = 0
    for step in range(max_steps):
        dismiss_reader_dialogs(page)
        prev_pos = get_reader_position(page)
        prev_src = page.evaluate(READER_PAGE_SOURCE)
        prev_frame = _capture_clean_reader_frame(page, timeout_ms=5000)
        prev_frame_hash = hashlib.sha256(prev_frame).hexdigest() if prev_frame is not None else None
        page.keyboard.press(reverse_key)
        page.wait_for_timeout(550)
        changed = False
        if prev_src:
            changed = wait_for_reader_turn(page, prev_src, timeout_ms=1200)
        cur_pos = get_reader_position(page)
        if cur_pos is not None and prev_pos is not None and cur_pos != prev_pos:
            changed = True
        if not changed and prev_frame_hash is not None:
            frame2 = _capture_clean_reader_frame(page, timeout_ms=5000)
            if frame2 is not None and hashlib.sha256(frame2).hexdigest() != prev_frame_hash:
                changed = True
            elif frame2 is not None:
                # Cover images can take slightly longer to paint after turning from a text page; re-check once.
                page.wait_for_timeout(450)
                frame3 = _capture_clean_reader_frame(page, timeout_ms=5000)
                if frame3 is not None and hashlib.sha256(frame3).hexdigest() != prev_frame_hash:
                    changed = True
        # If keyboard didn't move, ALWAYS try clicking the reverse chevron if it is still visible
        # (on Kindle Web Reader, the reverse chevron disappears only at the true cover, even if footer says Page 1).
        if not changed:
            try:
                chev = page.locator(reverse_chevron).first
                if chev.count() > 0 and chev.is_visible():
                    chev.click(force=True, timeout=1500)
                    page.wait_for_timeout(600)
                    cur_pos2 = get_reader_position(page)
                    if cur_pos2 is not None and prev_pos is not None and cur_pos2 != prev_pos:
                        changed = True
                        cur_pos = cur_pos2
                    elif prev_src and (page.evaluate(READER_PAGE_SOURCE) or "") != prev_src:
                        changed = True
                    elif prev_frame_hash is not None:
                        frame4 = _capture_clean_reader_frame(page, timeout_ms=5000)
                        if frame4 is not None and hashlib.sha256(frame4).hexdigest() != prev_frame_hash:
                            changed = True
            except Exception:
                pass
        if changed:
            unchanged = 0
            if (step + 1) % 20 == 0:
                log(f"[Reader] Rewinding to cover... step {step + 1} (pos={cur_pos})")
        else:
            unchanged += 1
            page.wait_for_timeout(300)
            if unchanged >= 2:
                log(f"[Reader] Reached true cover ({cur_pos}) after {step + 1} backward step(s).")
                return
    log(f"[Reader] Rewind stopped after {max_steps} steps.")


def is_reader_oops(page) -> bool:
    """Return True if paginated ebook reader is showing 'Oops... Something Went Wrong'."""
    try:
        back_btn = page.locator("button:has-text('Back to Library'), a:has-text('Back to Library')").first
        if back_btn.count() > 0 and back_btn.is_visible():
            return True
        body_text = (page.inner_text("body", timeout=1500) or "").lower()
        return "something went wrong" in body_text and "back to library" in body_text
    except Exception:
        return False


def get_reader_position(page) -> tuple[str, int] | None:
    """Read the footer's printed Page or Reader Location and keep the units distinct."""
    try:
        text = page.inner_text("body", timeout=1500) or ""
    except Exception:
        return None
    for kind, pattern in (
        ("page", r"Page\s+(\d[\d,]*)\s+of\s+\d[\d,]*"),
        ("page", r"\d[\d,]*\s*ページ中\s*(\d[\d,]*)\s*ページ"),
        ("location", r"Location\s+(\d[\d,]*)\s+of\s+\d[\d,]*"),
        ("location", r"位置\s*No\.?\s*(\d[\d,]*)"),
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return kind, int(match.group(1).replace(",", ""))
    return None


def get_reader_page_number(page) -> int | None:
    position = get_reader_position(page)
    return position[1] if position else None


def get_reader_total_pages(page) -> int | None:
    """Extract total page or location count from paginated ebook reader footer when available."""
    try:
        body_text = page.inner_text("body", timeout=1000) or ""
        m = re.search(r"Page\s+\d[\d,]*\s+of\s+(\d[\d,]*)", body_text, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))
        m_ja = re.search(r"(\d[\d,]*)\s*ページ中\s*\d[\d,]*\s*ページ", body_text)
        if m_ja:
            return int(m_ja.group(1).replace(",", ""))
        m_loc = re.search(r"Location\s+\d[\d,]*\s+of\s+(\d[\d,]*)", body_text, re.IGNORECASE)
        if m_loc:
            return int(m_loc.group(1).replace(",", ""))
    except Exception:
        pass
    return None


READER_HOSTS = {
    "read.amazon.co.jp", "read.amazon.com", "read.amazon.co.uk", "read.amazon.ca",
    "read.amazon.de", "read.amazon.fr", "read.amazon.it", "read.amazon.es",
    "read.amazon.com.au", "read.amazon.in", "read.amazon.com.br", "read.amazon.com.mx",
}


def inspect_reader_total_pages(url: str) -> int | None:
    """Read the book's page count from a temporary tab in the logged-in browser."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in READER_HOSTS or parsed.username or parsed.password:
        raise ValueError("Expected a paginated ebook reader URL")
    endpoint = resolve_cdp_url()
    if endpoint is None:
        raise RuntimeError("Open the app's login browser and sign in to the site first.")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0]
        page = context.new_page()
        page_target_id = _get_cdp_target_id(context, page)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            _wait_for_reader_reader(page, timeout_ms=8000)
            if get_reader_total_pages(page) is None:
                _reveal_reader_toolbar(page)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if is_reader_oops(page):
                    return None
                total = get_reader_total_pages(page)
                if total is not None:
                    return total
                page.wait_for_timeout(400)
            return None
        finally:
            _close_page_safely(page, endpoint, page_target_id)


def get_reader_progress_percent(page) -> int | None:
    """Extract reading progress percentage (0-100) from paginated ebook reader footer."""
    try:
        body_text = page.inner_text("body", timeout=1500) or ""
        m = re.search(r"[●•]\s*(\d{1,3})%", body_text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def verify_exported_pdf(
    pdf_path: Path,
    expected_pages: int | None = None,
    last_percent: int | None = None,
    captured_steps: int | None = None,
) -> dict:
    """Verify exported PDF for page count, blank pages, edge clipping, and readability margins."""
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        page_count = len(doc)
        blank_pages: list[int] = []
        clipped_pages: list[int] = []
        loading_pages: list[int] = []
        min_margin_ratio = 1.0

        for idx, page in enumerate(doc):
            if page_has_loading_or_error_text(page.get_text() or ""):
                loading_pages.append(idx + 1)
            pix = page.get_pixmap(dpi=72)
            w, h = pix.width, pix.height
            if w <= 0 or h <= 0:
                blank_pages.append(idx + 1)
                continue
            img = Image.frombytes("RGB" if pix.n >= 3 else "L", (w, h), pix.samples).convert("L")
            mask = img.point(lambda px: 255 if px < 245 else 0)
            bbox = mask.getbbox()
            if bbox is None:
                blank_pages.append(idx + 1)
                continue

            left, top, right, bottom = bbox
            if idx > 0:
                margin_ratio = min(
                    left / float(w),
                    top / float(h),
                    (w - right) / float(w),
                    (h - bottom) / float(h),
                )
                if margin_ratio < min_margin_ratio:
                    min_margin_ratio = margin_ratio

                if left <= 1 or top <= 1 or right >= w - 1 or bottom >= h - 1:
                    dark_mask = img.point(lambda px: 255 if px < 180 else 0)
                    db = dark_mask.getbbox()
                    if db and (db[0] <= 1 or db[1] <= 1 or db[2] >= w - 1 or db[3] >= h - 1):
                        clipped_pages.append(idx + 1)

        warnings: list[str] = []
        effective_count = captured_steps if captured_steps is not None else page_count
        if expected_pages is not None and effective_count < expected_pages:
            warnings.append(f"ページ不足({effective_count}/{expected_pages}p)")
        elif last_percent is not None and last_percent < 95:
            warnings.append(f"途中終了の疑い(進行率{last_percent}%)")
        if blank_pages:
            warnings.append(f"空白ページ({len(blank_pages)}p)")
        if loading_pages:
            warnings.append(f"読込/エラー画面混入(p.{','.join(str(p) for p in loading_pages[:5])})")
        if clipped_pages:
            warnings.append(f"端切れ/はみ出し疑い({len(clipped_pages)}p)")
        if page_count > 1 and min_margin_ratio < 0.02:
            warnings.append(f"余白狭い({min_margin_ratio * 100:.1f}%)")

        status = "OK" if not warnings else "WARN"
        page_info = f"{page_count}/{expected_pages}p" if expected_pages is not None else f"{page_count}p"
        margin_info = f"最小余白{min_margin_ratio * 100:.1f}%" if page_count > 1 else "余白N/A"
        summary = f"{status} ({page_info}, {margin_info}, 空白{len(blank_pages)}p, 切れ疑い{len(clipped_pages)}p)"
        if warnings:
            summary = f"{status}: {', '.join(warnings)} [{summary}]"

        return {
            "status": status,
            "page_count": page_count,
            "expected_pages": expected_pages,
            "blank_pages": blank_pages,
            "loading_pages": loading_pages,
            "clipped_pages": clipped_pages,
            "min_margin_ratio": min_margin_ratio,
            "summary": summary,
        }
    finally:
        doc.close()


class CaptureCanceled(BaseException):
    """Let cleanup run on a Stop request without treating it as a retryable reader error."""


class ReaderStepTimeout(TimeoutError):
    """Raised by SIGALRM watchdog when a single Reader page capture/turn hangs."""


def _handle_step_timeout(_signum, _frame) -> None:
    raise ReaderStepTimeout("Reader page step exceeded watchdog timeout")


def cancel_capture(_signum, _frame) -> None:
    signal.alarm(0)
    raise CaptureCanceled()


class IncompleteReaderCapture(RuntimeError):
    def __init__(self, message: str, page_count: int, expected_pages: int | None):
        super().__init__(message)
        self.page_count = page_count
        self.expected_pages = expected_pages


def check_reader_completeness(
    page_count: int,
    expected_pages: int | None,
    last_page: int | None,
    last_percent: int | None,
    max_pages: int,
    skipped_blank_steps: int = 0,
) -> None:
    if expected_pages is not None:
        if last_page is not None:
            # Allow a 1-2 page trailing margin at >=98% (e.g. intentionally blank verso or Kindle end-of-book screen)
            # as well as skipped intentionally blank pages.
            at_tail = (
                (last_page + skipped_blank_steps >= expected_pages)
                or (expected_pages > 0 and (last_page / expected_pages) >= 0.98)
                or (last_percent is not None and last_percent >= 98 and last_page >= expected_pages - max(2, skipped_blank_steps))
            )
            incomplete = not at_tail
        else:
            incomplete = (page_count + max(1, skipped_blank_steps)) < expected_pages
    else:
        incomplete = last_percent is not None and last_percent < 95
    if incomplete:
        message = "最大ページ数に達しました。上限を増やしてください。" if page_count >= max_pages else "本の最後まで取得できませんでした。"
        raise IncompleteReaderCapture(message, page_count, expected_pages)


@dataclass(frozen=True)
class ReaderCapture:
    pdf: bytes
    position: int | None
    image_hash: str | None
    total: int | None
    percent: int | None
    viewport_height: int | None


class ReaderCheckpoint:
    """Save each captured page and its reader position in the same transaction."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[ReaderCapture]:
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages'").fetchone() is None:
                return []
            rows = db.execute(
                "SELECT pdf, position, image_hash, total, percent, viewport_height "
                "FROM pages ORDER BY ordinal"
            )
            return [ReaderCapture(*row) for row in rows]

    def append(self, ordinal: int, capture: ReaderCapture) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pages (ordinal INTEGER PRIMARY KEY, pdf BLOB NOT NULL, "
                "position INTEGER, image_hash TEXT, total INTEGER, percent INTEGER, viewport_height INTEGER)"
            )
            db.execute(
                "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ordinal, capture.pdf, capture.position, capture.image_hash,
                 capture.total, capture.percent, capture.viewport_height),
            )

    def progress(self) -> tuple[int, int | None]:
        if not self.path.is_file():
            return 0, None
        try:
            with closing(sqlite3.connect(self.path)) as db:
                row = db.execute("SELECT ordinal, total FROM pages ORDER BY ordinal DESC LIMIT 1").fetchone()
                return row if row is not None else (0, None)
        except sqlite3.Error:
            return 0, None

    def verified_gaps(self) -> dict[int, bytes]:
        with closing(sqlite3.connect(self.path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verified_gaps'").fetchone() is None:
                return {}
            return dict(db.execute("SELECT ordinal, pdf FROM verified_gaps"))

    def save_verified_gap(self, ordinal: int, pdf: bytes) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS verified_gaps (ordinal INTEGER PRIMARY KEY, pdf BLOB NOT NULL)")
            db.execute("INSERT OR REPLACE INTO verified_gaps VALUES (?, ?)", (ordinal, pdf))

    def final_path(self, proposed: Path) -> Path:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS export_path (path TEXT NOT NULL)")
            existing = db.execute("SELECT path FROM export_path").fetchone()
            if existing:
                return Path(existing[0])
            db.execute("INSERT INTO export_path VALUES (?)", (str(proposed),))
            return proposed

    def save_resume_url(self, url: str, skipped_steps: int = 0) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS resume_state "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), url TEXT NOT NULL, skipped_steps INTEGER DEFAULT 0)"
            )
            cols = {row[1] for row in db.execute("PRAGMA table_info(resume_state)")}
            if "skipped_steps" not in cols:
                db.execute("ALTER TABLE resume_state ADD COLUMN skipped_steps INTEGER DEFAULT 0")
            db.execute(
                "INSERT OR REPLACE INTO resume_state (id, url, skipped_steps) VALUES (1, ?, ?)",
                (url, skipped_steps),
            )

    def load_resume_state(self) -> tuple[str | None, int]:
        if not self.path.is_file():
            return None, 0
        try:
            with closing(sqlite3.connect(self.path)) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resume_state'").fetchone() is None:
                    return None, 0
                cols = {row[1] for row in db.execute("PRAGMA table_info(resume_state)")}
                if "skipped_steps" in cols:
                    row = db.execute("SELECT url, COALESCE(skipped_steps, 0) FROM resume_state WHERE id = 1").fetchone()
                    return (str(row[0]) if row and row[0] else None), (int(row[1]) if row else 0)
                row = db.execute("SELECT url FROM resume_state WHERE id = 1").fetchone()
                return (str(row[0]) if row and row[0] else None), 0
        except sqlite3.Error:
            return None, 0

    def load_resume_url(self) -> str | None:
        url, _ = self.load_resume_state()
        return url

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


def _wait_for_reader_reader(page, timeout_ms: int = 15000, previous_source: str = "") -> bool:
    """Wait until ebook reader renders a blob page image or shows an Oops dialog."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        dismiss_reader_dialogs(page)
        if is_reader_oops(page):
            return False
        source = page.evaluate(READER_PAGE_SOURCE)
        if source and source != previous_source:
            return True
        page.wait_for_timeout(250)
    return False


READER_PAGE_SOURCE = """() => {
    const images = [...document.querySelectorAll('img[src^="blob:"]')];
    const pages = images.map(img => ({img, box: img.getBoundingClientRect()}))
        .filter(({img, box}) => img.complete && img.naturalWidth
            && box.width > 100 && box.height > 100 && box.right > 0 && box.bottom > 0
            && box.left < innerWidth && box.top < innerHeight);
    pages.sort((a, b) => b.box.width * b.box.height - a.box.width * a.box.height);
    return pages[0]?.img.src || '';
}"""


def wait_for_reader_turn(page, previous_source: str, timeout_ms: int = 5000) -> bool:
    """Wait for a decoded page image, or return as soon as the error dialog appears."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if is_reader_oops(page):
            return False
        source = page.evaluate(READER_PAGE_SOURCE)
        if source and source != previous_source:
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


READER_TOC_NAME = re.compile(r"^(?:Table of Contents|Contents|目次)$", re.IGNORECASE)


def jump_to_reader_chapter(page, offset: int) -> bool:
    """Open the reader's TOC and select the current or immediately following chapter."""
    previous_source = page.evaluate(READER_PAGE_SOURCE)
    toggle = page.get_by_role("button", name=READER_TOC_NAME).or_(
        page.locator("#kr-toc-button, #reader-toc-button, [data-testid='toc-button']")
    )
    for button in toggle.all():
        if button.is_visible():
            button.click(timeout=2000)
            break
    else:
        return False

    heading = page.get_by_role("heading", name=READER_TOC_NAME).filter(visible=True).first
    if not heading.count():
        heading = page.get_by_text(READER_TOC_NAME, exact=True).filter(visible=True).last
    try:
        heading.wait_for(state="visible", timeout=2000)
        panel = heading.locator("xpath=..")
        entry_selector = "button, a, [role=option], [role=menuitem], [role=button], li[tabindex]"
        for _ in range(4):
            if panel.evaluate("node => node === document.body || node === document.documentElement"):
                break
            target_index = panel.evaluate("""(panel, {selector, offset}) => {
                const entries = [...panel.querySelectorAll(selector)];
                const chapters = entries.filter(node =>
                    node.getClientRects().length && node.textContent.trim() &&
                    !node.querySelector(selector) && !node.disabled &&
                    node.getAttribute('aria-disabled') !== 'true'
                );
                const selected = chapters.findIndex(node => {
                    const active = node.closest(
                        '[aria-current]:not([aria-current="false"]), [aria-selected="true"], '
                        + '[data-selected="true"], .selected, [class*="toc-item-selected" i]'
                    );
                    return active && active !== panel && panel.contains(active);
                });
                if (selected < 0) return null;
                return entries.indexOf(chapters[selected + offset]);
            }""", {"selector": entry_selector, "offset": offset})
            if target_index is not None:
                if target_index < 0:
                    return False
                panel.locator(entry_selector).nth(target_index).click(timeout=3000)
                # The menu must not obscure either the chapter or the checkpoint image.
                heading.wait_for(state="hidden", timeout=2500)
                if offset == 0 and page.evaluate(READER_PAGE_SOURCE) and not is_reader_oops(page):
                    return True
                return wait_for_reader_turn(page, previous_source)
            panel = panel.locator("xpath=..")
        return False
    finally:
        if heading.is_visible() and button.is_visible():
            button.click(timeout=2000)


READER_GO_TO_PAGE = re.compile(r"^(?:Go to Page|ページ[へに]移動)$", re.IGNORECASE)
READER_GO_TO_LOCATION = re.compile(r"^(?:Go to Location|位置(?:番号)?[へに]移動)$", re.IGNORECASE)


def reader_number_fields(panel):
    field = panel.locator("input[type=number], input[placeholder='location' i], input[placeholder='page' i]").filter(visible=True)
    go = panel.get_by_text(re.compile(r"^(?:Go|移動)$", re.I), exact=True).filter(visible=True)
    if field.count() == 1 and go.count() == 1:
        return field, go
    return None


def open_reader_number_dialog(page, prefer_page: bool):
    # The Ionic reader has an explicit modal. Its Go control is an ion-button host.
    live_panel = page.locator("ion-modal.go-to-modal").filter(visible=True).first
    if live_panel.is_visible():
        controls = reader_number_fields(live_panel)
        if controls:
            kind = "page" if live_panel.get_by_text(READER_GO_TO_PAGE, exact=True).first.is_visible() else "location"
            return kind, live_panel, *controls

    location = page.get_by_text(READER_GO_TO_LOCATION, exact=True).filter(visible=True).first
    printed_page = page.get_by_text(READER_GO_TO_PAGE, exact=True).filter(visible=True).first
    available = printed_page.or_(location) if prefer_page else location
    if not available.first.is_visible():
        menu_name = re.compile(
            r"^(?:More(?: options| menu| actions| settings)?|Reader menu|Menu|Options|その他(?:のオプション)?|詳細(?:設定)?|メニュー|⋮)$",
            re.I,
        )
        reader_menu = page.locator("ion-button[aria-label='Reader menu'], #reader-header [aria-label='メニュー']").filter(visible=True)
        if not reader_menu.count() and urlparse(page.url).hostname in READER_HOSTS:
            # The top bar is actually removed until the reader canvas is tapped.
            box = _find_reader_page_box(page)
            if box is not None:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                try:
                    reader_menu.first.wait_for(state="visible", timeout=2000)
                except Exception:
                    pass
        menus = reader_menu.or_(page.get_by_role("button", name=menu_name)).or_(
            page.locator(
                "#kr-more, #kr-more-menu, #kr-more-menu-button, #kr-more-settings, "
                "[data-testid='more-button'], button[aria-haspopup='menu'], [role='button'][aria-haspopup='menu']"
            )
        ).filter(visible=True)
        for menu in menus.all()[:4]:
            menu.click(timeout=1500)
            try:
                available.first.wait_for(state="visible", timeout=900)
                break
            except Exception:
                if menu.is_visible() and menu.get_attribute("aria-expanded") == "true":
                    menu.click(timeout=1500)
                else:
                    page.keyboard.press("Escape")
        else:
            return None

    if prefer_page and printed_page.is_visible():
        kind, entry, label = "page", printed_page, READER_GO_TO_PAGE
    elif location.is_visible():
        kind, entry, label = "location", location, READER_GO_TO_LOCATION
    else:
        return None
    entry.click(timeout=2000)
    if live_panel.is_visible():
        controls = reader_number_fields(live_panel)
        if controls:
            return kind, live_panel, *controls
    heading = page.get_by_text(label, exact=True).filter(visible=True).last
    heading.wait_for(state="visible", timeout=2000)
    panel = heading.locator("xpath=..")
    for _ in range(4):
        if panel.evaluate("node => node === document.body || node === document.documentElement"):
            break
        controls = reader_number_fields(panel)
        if controls:
            return kind, panel, *controls
        panel = panel.locator("xpath=..")
    page.keyboard.press("Escape")
    return None


def seek_reader_position(
    page, target: int, target_is_printed_page: bool, expected_total_pages: int | None = None,
) -> bool:
    """Navigate by number first; the caller still verifies the saved screenshot."""
    low, high = 1, None
    next_probe = None
    locations_per_page = None
    for attempt in range(13 if target_is_printed_page else 1):
        before = get_reader_position(page)
        dialog = open_reader_number_dialog(page, prefer_page=target_is_printed_page)
        if dialog is None:
            log("[Reader] No usable Go to Page or Go to Location dialog was found.")
            return False
        kind, panel, field, go = dialog
        try:
            same_position = before == (kind, target)
            if kind == "page" or not target_is_printed_page:
                number = target
            elif (before and before[0] == "page") or expected_total_pages:
                # The input is in locations even though the checkpoint stores a printed page.
                # Re-enter the location shown for this page when we are already on the label.
                label = panel.inner_text(timeout=1500)
                current_location = re.search(r"(?:on location|現在の位置[:：]?)\s*(\d[\d,]*)", label, re.I)
                if before == ("page", target) and current_location:
                    number = int(current_location.group(1).replace(",", ""))
                    same_position = True
                else:
                    if high is None:
                        maximum = field.get_attribute("max", timeout=1000)
                        if not maximum or not maximum.isdigit():
                            bounds = re.search(r"(?:\bof\s+|全\s*)(\d[\d,]*)", label, re.I)
                            maximum = bounds.group(1).replace(",", "") if bounds else None
                        if not maximum or int(maximum) < 1:
                            log("[Reader] The maximum location number is unavailable.")
                            return False
                        high = int(maximum)
                        if expected_total_pages and expected_total_pages > 0:
                            locations_per_page = high / expected_total_pages
                            next_probe = round((target - 1) * locations_per_page)
                    if low > high:
                        return False
                    midpoint = (low + high) // 2
                    number = next_probe if next_probe is not None and low <= next_probe <= high else midpoint
            else:
                log("[Reader] The reader has no printed page label to compare with the saved page.")
                return False

            previous_source = page.evaluate(READER_PAGE_SOURCE)
            log(f"[Reader] Entering {kind} number {number} to recover the saved position.")
            field.fill(str(number), timeout=1500)
            go.click(timeout=2000)
            field.wait_for(state="hidden", timeout=2000)
        finally:
            if field.is_visible():
                cancel = panel.get_by_text(re.compile(r"^(?:Cancel|キャンセル)$", re.I), exact=True).filter(visible=True).first
                if cancel.is_visible():
                    cancel.click(timeout=1500)

        changed = wait_for_reader_turn(page, previous_source, timeout_ms=1000 if same_position else 3500)
        if not changed and (not same_position or is_reader_oops(page)):
            return False
        actual = get_reader_position(page)
        if kind == "page":
            deadline = time.monotonic() + 4
            while not actual or actual[0] != "page" or abs(actual[1] - number) > 1:
                if time.monotonic() >= deadline or is_reader_oops(page):
                    log(f"[Reader] The page field did not settle near {number}.")
                    return False
                page.wait_for_timeout(250)
                actual = get_reader_position(page)
        if actual:
            log(f"[Reader] Number navigation reached {actual[0]} {actual[1]}.")
        if kind == "page" or not target_is_printed_page or same_position:
            return True
        if actual is None or actual[0] != "page":
            # The cover can report Location only. The first probe seeks into
            # the body where printed page labels are usually available.
            if attempt == 0 and locations_per_page and high is not None:
                next_probe = min(high, number + max(1, round(locations_per_page * 3)))
                if next_probe != number:
                    continue
            return False
        if abs(actual[1] - target) <= 1:
            return True
        if actual[1] < target:
            low = number + 1
        else:
            high = number - 1
        next_probe = None
        if locations_per_page and attempt < 2:
            next_probe = number + round((target - actual[1]) * locations_per_page)
    return False


def reopen_reader_from_library(page, start_url: str, book_title: str | None) -> bool:
    """Use the reader's library button, selecting the exact book by ASIN or title."""
    library_name = re.compile(r"^(Back to Library|Kindle Library|ライブラリに戻る|Kindleライブラリ)$", re.I)
    back = page.get_by_role("button", name=library_name).or_(
        page.get_by_role("link", name=library_name)
    ).filter(visible=True).first
    if not back.is_visible():
        return False

    previous_source = page.evaluate(READER_PAGE_SOURCE)
    back.click(timeout=3000)
    asin = parse_qs(urlparse(start_url).query).get("asin", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9]+", asin):
        return False
    asin_card = page.locator(
        f"[data-asin='{asin}'], [id='{asin}'], a[href*='asin={asin}'], a[href$='/{asin}']"
    ).filter(visible=True).first
    if book_title:
        title_card = page.get_by_text(book_title, exact=True).filter(visible=True)
        asin_card.or_(title_card).first.wait_for(state="visible", timeout=6000)
    else:
        asin_card.wait_for(state="visible", timeout=6000)

    if asin_card.is_visible():
        card = asin_card
    elif book_title and title_card.count() == 1:
        card = title_card
    else:
        log("[Reader] Could not identify a unique library entry for this book.")
        return False
    card.click(timeout=3000)
    if not _wait_for_reader_reader(page, previous_source=previous_source):
        return False
    opened_url = urlparse(page.url)
    if opened_url.hostname != urlparse(start_url).hostname:
        return False
    opened_asin = parse_qs(opened_url.query).get("asin", [""])[0]
    if opened_asin and opened_asin.upper() != asin.upper():
        log("[Reader] The library opened a different book.")
        return False
    log("[Reader] Reopened the book from the library.")
    return True


def matches_saved_reader_image(screenshot: bytes, saved_pdf: bytes, *, same_printed_page: bool = False) -> bool:
    """Compare a clipped view to the single embedded image in its checkpoint PDF."""
    import pymupdf

    try:
        with Image.open(io.BytesIO(screenshot)) as current, pymupdf.open(
            stream=saved_pdf, filetype="pdf"
        ) as doc:
            images = doc[0].get_images()
            if len(doc) != 1 or len(images) != 1:
                return False
            saved_image = doc.extract_image(images[0][0])["image"]
            with Image.open(io.BytesIO(saved_image)) as saved:
                w, h = current.size
                pad_x, pad_y = max(24, round(w * 0.07)), max(32, round(h * 0.07))
                if saved.size == (w + 2 * pad_x, h + 2 * pad_y):
                    saved = saved.crop((pad_x, pad_y, pad_x + w, pad_y + h))
                else:
                    # Handle trimmed/padded captures with custom margin ratios
                    cur_trim = _trim_outer_white_margin(current.convert("RGB"), pad_ratio=0.0)
                    sav_trim = _trim_outer_white_margin(saved.convert("RGB"), pad_ratio=0.0)
                    current = cur_trim
                    saved = sav_trim
                size = (128, 128)
                current_luma, saved_luma = current.convert("L").resize(size), saved.convert("L").resize(size)
                if same_printed_page:
                    current_median, saved_median = (ImageStat.Stat(img).median[0] for img in (current_luma, saved_luma))
                    if min(current_median, saved_median) < 48 and max(current_median, saved_median) > 207:
                        current_luma = ImageOps.invert(current_luma)
                difference = ImageChops.difference(current_luma, saved_luma)
                max_mean = 1.8 if same_printed_page else 0.45
                max_tile_mean = 5.5 if same_printed_page else 1.6
                if ImageStat.Stat(difference).mean[0] > max_mean:
                    return False
                # A local change (such as a different paragraph) can disappear
                # in an average over a mostly empty page.
                for y in range(0, 128, 16):
                    for x in range(0, 128, 16):
                        if ImageStat.Stat(difference.crop((x, y, x + 16, y + 16))).mean[0] > max_tile_mean:
                            return False
                return True
    except Exception:
        return False


def _take_screenshot_resilient(
    page,
    *,
    clip: dict | None = None,
    timeout_ms: int = 12000,
    cdp_session=None,
) -> bytes:
    """Take a screenshot, clamping clip bounds to the viewport and falling back to CDP Page.captureScreenshot."""
    safe_clip = clip
    if safe_clip is not None:
        try:
            vp = page.viewport_size
            if vp and vp.get("width") and vp.get("height"):
                vp_w = float(vp["width"])
                vp_h = float(vp["height"])
                x = max(0.0, float(safe_clip["x"]))
                y = max(0.0, float(safe_clip["y"]))
                x2 = min(vp_w, float(safe_clip["x"]) + float(safe_clip["width"]))
                y2 = min(vp_h, float(safe_clip["y"]) + float(safe_clip["height"]))
                if x2 - x >= 10.0 and y2 - y >= 10.0:
                    safe_clip = {"x": x, "y": y, "width": x2 - x, "height": y2 - y}
                else:
                    safe_clip = None
        except Exception:
            safe_clip = None
    try:
        if safe_clip is not None:
            return page.screenshot(clip=safe_clip, animations="disabled", timeout=timeout_ms)
        return page.screenshot(full_page=False, animations="disabled", timeout=timeout_ms)
    except Exception as exc:
        err_low = str(exc).lower()
        if safe_clip is not None and ("clipped area" in err_low or "outside" in err_low or "empty" in err_low):
            return page.screenshot(full_page=False, animations="disabled", timeout=timeout_ms)
        if "Timeout" not in type(exc).__name__ and "timeout" not in err_low:
            raise
        log(f"[Reader] Playwright screenshot timed out ({type(exc).__name__}); falling back to CDP Page.captureScreenshot.")
        import base64

        sess = cdp_session or page.context.new_cdp_session(page)
        try:
            params: dict = {"format": "png", "fromSurface": True}
            if safe_clip is not None:
                params["clip"] = {
                    "x": float(safe_clip["x"]),
                    "y": float(safe_clip["y"]),
                    "width": float(safe_clip["width"]),
                    "height": float(safe_clip["height"]),
                    "scale": 1.0,
                }
            res = sess.send("Page.captureScreenshot", params)
            return base64.b64decode(res["data"])
        finally:
            if cdp_session is None:
                try:
                    sess.detach()
                except Exception:
                    pass


def _capture_clean_reader_frame(page, timeout_ms: int = 12000) -> bytes | None:
    """Capture the Reader page view with Kindle UI chrome hidden, matching normal page capture."""
    _hide_reader_chrome(page)
    try:
        box = _find_reader_page_box(page)
        if box is None:
            return None
        return _take_screenshot_resilient(page, clip=box, timeout_ms=timeout_ms)
    finally:
        _restore_reader_chrome(page)


def _matches_saved_reader_view(
    page,
    last_image_hash: str | None,
    last_pdf: bytes | None,
    last_reader_page: int | None,
    expected_position_kind: str | None,
) -> bool:
    """Check whether the currently displayed Reader view matches the last saved checkpoint page."""
    marker = get_reader_position(page)
    position = marker[1] if marker and expected_position_kind in (None, marker[0]) else None
    matches_position = last_reader_page is None or position is None or position == last_reader_page
    if not matches_position:
        return False
    if last_image_hash is None:
        return last_reader_page is not None and position == last_reader_page
    frame = _capture_clean_reader_frame(page, timeout_ms=15000)
    if frame is None:
        return False
    if hashlib.sha256(frame).hexdigest() == last_image_hash:
        return True
    if last_pdf is not None:
        same_printed = (
            last_reader_page is not None
            and marker is not None
            and marker[0] == expected_position_kind
            and position == last_reader_page
        )
        if matches_saved_reader_image(frame, last_pdf, same_printed_page=same_printed):
            log("[Reader] Verified the saved page by matching its image.")
            return True
    return False


def _step_reader_once(page, key: str) -> bool:
    """Press a turn key once and wait for either DOM/source change or a short settle delay."""
    source = page.evaluate(READER_PAGE_SOURCE)
    page.keyboard.press(key)
    if source:
        if not wait_for_reader_turn(page, source):
            return False
    else:
        page.wait_for_timeout(450)
    return not is_reader_oops(page)


def _search_nearby_for_saved_view(
    page,
    turn_key: str,
    reverse_key: str,
    captured_count: int,
    last_image_hash: str | None,
    last_pdf: bytes | None,
    last_reader_page: int | None,
    expected_position_kind: str | None,
    radius: int = 8,
) -> bool:
    """Check current view and +/- radius pages around it before doing any full rewind."""
    if is_reader_oops(page):
        return False
    if _matches_saved_reader_view(page, last_image_hash, last_pdf, last_reader_page, expected_position_kind):
        if _step_reader_once(page, turn_key):
            log(f"[Reader] Resumed immediately from current view after captured page {captured_count}.")
            return True
        return False

    # Search backward up to `radius` pages (Kindle often reopens 1-3 pages ahead or behind).
    steps_back = 0
    for _ in range(radius):
        if not _step_reader_once(page, reverse_key):
            break
        steps_back += 1
        if _matches_saved_reader_view(page, last_image_hash, last_pdf, last_reader_page, expected_position_kind):
            if _step_reader_once(page, turn_key):
                log(f"[Reader] Resumed after captured page {captured_count} (found {steps_back} step(s) back).")
                return True
            return False

    # Return to origin and search forward up to `radius` pages.
    for _ in range(steps_back):
        if not _step_reader_once(page, turn_key):
            return False
    for step_fwd in range(1, radius + 1):
        if not _step_reader_once(page, turn_key):
            break
        if _matches_saved_reader_view(page, last_image_hash, last_pdf, last_reader_page, expected_position_kind):
            if _step_reader_once(page, turn_key):
                log(f"[Reader] Resumed after captured page {captured_count} (found {step_fwd} step(s) ahead).")
                return True
            return False
    return False


def recover_reader_oops(
    page,
    start_url: str,
    last_reader_page: int | None,
    turn_key: str,
    captured_count: int = 0,
    last_image_hash: str | None = None,
    toc_offset: int | None = None,
    book_title: str | None = None,
    fresh_tab: bool = False,
    use_location: bool = False,
    expected_total_pages: int | None = None,
    last_pdf: bytes | None = None,
) -> bool:
    """Reopen the book and resume after the last verified capture."""
    try:
        opened_from_library = False
        if not fresh_tab:
            log("[Reader] Trying to reopen the book from the library...")
            try:
                opened_from_library = reopen_reader_from_library(page, start_url, book_title)
            except Exception as exc:
                log(f"[Reader] Library reopen did not finish ({type(exc).__name__}).")
        if not opened_from_library:
            response = page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            check_access_denied(page, response.status if response else None)
            if not _wait_for_reader_reader(page):
                log("[Reader] The reopened reader did not render a page.")
                return False

        if captured_count == 0:
            if is_reader_not_at_start(page):
                rewind_reader_to_start(page, turn_key)
            return _wait_for_reader_reader(page)

        reverse_key = "ArrowRight" if turn_key == "ArrowLeft" else "ArrowLeft"
        expected_position_kind = "page" if expected_total_pages is not None else None

        # 1. First check right where Kindle reopened (and +/- 8 pages around it) before jumping or rewinding.
        if last_image_hash is not None:
            log("[Reader] Checking reopened position and nearby pages for the last saved view...")
            if _search_nearby_for_saved_view(
                page, turn_key, reverse_key, captured_count,
                last_image_hash, last_pdf, last_reader_page, expected_position_kind, radius=8,
            ):
                return True
            if is_reader_oops(page):
                return False

        # 2. Try Go to Page / Go to Location using printed page or captured_count fallback.
        jump_target = last_reader_page if last_reader_page is not None else (captured_count if captured_count > 0 else None)
        if use_location and jump_target is not None and last_image_hash is not None:
            try:
                log(f"[Reader] Trying number navigation (target={jump_target}) before table of contents or rewind...")
                seek_reader_position(
                    page, jump_target, expected_total_pages is not None, expected_total_pages,
                )
            except Exception as exc:
                log(f"[Reader] Go to Location unavailable ({type(exc).__name__}).")
                page.keyboard.press("Escape")
            if is_reader_oops(page):
                return False
            if _search_nearby_for_saved_view(
                page, turn_key, reverse_key, captured_count,
                last_image_hash, last_pdf, last_reader_page, expected_position_kind, radius=10,
            ):
                return True
            if is_reader_oops(page):
                return False

        # 3. Try Table of Contents jump (even when printed page numbers are unavailable).
        if toc_offset is not None and last_image_hash is not None:
            log("[Reader] Trying the table of contents to recover the page turn...")
            try:
                jumped = jump_to_reader_chapter(page, toc_offset)
            except Exception as exc:
                log(f"[Reader] Table of contents unavailable ({type(exc).__name__}).")
                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                if not _wait_for_reader_reader(page):
                    return False
                jumped = False
            if jumped and _search_nearby_for_saved_view(
                page, turn_key, reverse_key, captured_count,
                last_image_hash, last_pdf, last_reader_page, expected_position_kind, radius=12,
            ):
                return True
            if is_reader_oops(page):
                return False

        # 4. Last resort: rewind to start when footer position is unavailable and walk forward.
        if last_reader_page is None or get_reader_page_number(page) is None:
            rewind_reader_to_start(page, turn_key)
        previous_direction = None
        reversals = 0
        for _ in range(max(captured_count + 20, 40)):
            if is_reader_oops(page):
                return False
            marker = get_reader_position(page)
            position = marker[1] if marker and expected_position_kind in (None, marker[0]) else None
            if _matches_saved_reader_view(page, last_image_hash, last_pdf, last_reader_page, expected_position_kind):
                if _step_reader_once(page, turn_key):
                    log(f"[Reader] Resumed after captured page {captured_count}.")
                    return True
                return False
            backwards = last_reader_page is not None and position is not None and position > last_reader_page
            direction = reverse_key if backwards else turn_key
            if previous_direction is not None and direction != previous_direction:
                reversals += 1
                if reversals > 1:
                    log("[Reader] Stopped seeking after crossing the saved page without verifying its image.")
                    return False
            previous_direction = direction
            if not _step_reader_once(page, direction):
                return False
        log("[Reader] Could not verify the last captured page after reopening.")
        return False
    except Exception as exc:
        log(f"[Reader] Reader recovery failed ({type(exc).__name__}).")
        return False


READER_VIEWPORT_WIDTH = 1020
READER_LANDSCAPE_VIEWPORT_WIDTH = 1360
READER_DEFAULT_HEIGHT = 1360

READER_ASPECT_PRESETS: dict[str, tuple[int, int]] = {
    "ipad": (1020, 1360),
    "iphone": (760, 1350),
    "pc": (1360, 1020),
}

READER_CHROME_HIDE_SCRIPT = """() => {
    const selectors = [
        'header', 'footer', 'nav', 'ion-header', 'ion-footer', 'ion-toolbar',
        '#reader-header', '#reader-footer',
        '#kr-chevron-left', '#kr-chevron-right', '[class*="chevron" i]',
        '[class*="kr-header" i]', '[class*="kr-footer" i]',
        '[class*="scrubber" i]', '[role="slider"]', 'ion-range',
        '[class*="bookmark" i]', '[aria-label*="bookmark" i]', '[aria-label*="ブックマーク" i]',
        '[aria-label*="Back to" i]', '[aria-label="Reader menu"]', '[aria-label="メニュー"]'
    ];
    for (const sel of selectors) {
        document.querySelectorAll(sel).forEach(el => {
            if (!el.hasAttribute('data-ec-reader-hide')) {
                el.setAttribute('data-ec-reader-hide', el.style.display || '');
                el.style.setProperty('display', 'none', 'important');
            }
        });
    }
    const vh = window.innerHeight || 0;
    const vw = window.innerWidth || 0;
    document.querySelectorAll('*').forEach(el => {
        if (el.hasAttribute('data-ec-reader-hide')) return;
        const tag = el.tagName;
        if (tag === 'HTML' || tag === 'BODY' || tag === 'IMG' || tag === 'CANVAS' || tag === 'SCRIPT' || tag === 'STYLE') return;
        if (el.querySelector('img[src^="blob:"], canvas')) return;
        const st = window.getComputedStyle(el);
        if (st.position !== 'fixed' && st.position !== 'absolute' && st.position !== 'sticky') return;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return;
        const isTopBar = r.top <= 30 && r.height <= 160 && r.width >= 200;
        const isBottomBar = vh > 0 && r.bottom >= vh - 30 && r.top >= vh - 200 && r.height <= 200 && r.width >= 200;
        const isBottomWidget = vh > 0 && r.bottom >= vh - 160 && r.top >= vh - 200 && r.height <= 120 && r.width <= 200 && (r.left <= 120 || (vw > 0 && r.right >= vw - 120));
        if (isTopBar || isBottomBar || isBottomWidget) {
            el.setAttribute('data-ec-reader-hide', el.style.display || '');
            el.style.setProperty('display', 'none', 'important');
        }
    });
}"""

READER_CHROME_RESTORE_SCRIPT = """() => {
    document.querySelectorAll('[data-ec-reader-hide]').forEach(el => {
        el.style.display = el.getAttribute('data-ec-reader-hide');
        el.removeAttribute('data-ec-reader-hide');
    });
}"""


def _hide_reader_chrome(page) -> None:
    for scope in [page, *getattr(page, "frames", [])]:
        try:
            scope.evaluate(READER_CHROME_HIDE_SCRIPT)
        except Exception:
            pass


def _restore_reader_chrome(page) -> None:
    for scope in [page, *getattr(page, "frames", [])]:
        try:
            scope.evaluate(READER_CHROME_RESTORE_SCRIPT)
        except Exception:
            pass


def _find_reader_page_box_in_scope(scope) -> dict | None:
    """Return the bounding box of the largest visible Reader content element in scope."""
    selectors = [
        ("img[src^='blob:']", True),
        ("canvas", False),
        ("img:not(header img):not(nav img):not(footer img):not(ion-header img):not(ion-footer img)", True),
    ]
    for selector, check_img in selectors:
        try:
            items = scope.locator(selector)
            count = items.count()
            best_box = None
            best_area = 0.0
            for i in range(count):
                el = items.nth(i)
                if check_img:
                    if not el.evaluate("(img) => img.complete && img.naturalWidth > 100 && img.naturalHeight > 100"):
                        continue
                else:
                    if not el.evaluate("(c) => c.width > 100 && c.height > 100"):
                        continue
                box = el.bounding_box(timeout=1000)
                if not box:
                    continue
                w, h = box.get("width", 0.0), box.get("height", 0.0)
                if w > 150 and h > 150 and (w * h) > best_area:
                    best_area = w * h
                    best_box = box
            if best_box is not None:
                return best_box
        except Exception:
            continue
    return None


def _find_reader_page_box(page) -> dict | None:
    """Return the bounding box of the largest visible Reader page image or canvas."""
    box = _find_reader_page_box_in_scope(page)
    if box is not None:
        return box
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            box = _find_reader_page_box_in_scope(frame)
            if box is not None:
                return box
    except Exception:
        pass
    return None


def _trim_outer_white_margin(rgb: Image.Image, pad_ratio: float = 0.015) -> Image.Image:
    """Crop excessive outer white borders while keeping a small clean margin."""
    try:
        bbox = rgb.point(lambda x: 0 if x > 246 else 255).getbbox()
        if bbox:
            cw = bbox[2] - bbox[0]
            ch = bbox[3] - bbox[1]
            if cw > 100 and ch > 100:
                rgb = rgb.crop(bbox)
    except Exception:
        pass
    if pad_ratio > 0:
        pad_x = max(8, int(round(rgb.width * pad_ratio)))
        pad_y = max(10, int(round(rgb.height * pad_ratio)))
        canvas = Image.new("RGB", (rgb.width + 2 * pad_x, rgb.height + 2 * pad_y), (255, 255, 255))
        canvas.paste(rgb, (pad_x, pad_y))
        return canvas
    return rgb


def _apply_reader_viewport(page, width: int, height: int, scale_factor: int = 1, cdp_session=None) -> None:
    """Set viewport size and optional 2x Retina deviceScaleFactor via CDP Emulation."""
    page.set_viewport_size({"width": width, "height": height})
    if scale_factor > 1:
        try:
            sess = cdp_session or page.context.new_cdp_session(page)
            sess.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": scale_factor,
                    "mobile": False,
                },
            )
            if cdp_session is None:
                try:
                    sess.detach()
                except Exception:
                    pass
        except Exception:
            pass


def capture_reader_page_as_pdf_bytes(
    page,
    pad_ratio: float = 0.0,
    cdp_session=None,
    scale_factor: int = 1,
) -> tuple[bytes, bytes] | None:
    """Capture the rendered Reader page via clipped screenshot with uniform clean margins."""
    _hide_reader_chrome(page)
    try:
        box = _find_reader_page_box(page)
        if not box:
            return None
        png_bytes = _take_screenshot_resilient(page, clip=box, timeout_ms=12000, cdp_session=cdp_session)
    finally:
        _restore_reader_chrome(page)

    buf = io.BytesIO()
    with Image.open(io.BytesIO(png_bytes)) as img:
        rgb = img.convert("RGB")
        dpi = 72.0 * max(1, scale_factor)
        if pad_ratio > 0:
            pad_x = max(8, int(round(rgb.width * pad_ratio)))
            pad_y = max(10, int(round(rgb.height * pad_ratio)))
            canvas = Image.new("RGB", (rgb.width + 2 * pad_x, rgb.height + 2 * pad_y), (255, 255, 255))
            canvas.paste(rgb, (pad_x, pad_y))
            canvas.save(buf, "PDF", resolution=dpi)
        else:
            rgb.save(buf, "PDF", resolution=dpi)
    return buf.getvalue(), png_bytes


def measure_cover_aspect_ratio(png_bytes: bytes) -> float | None:
    """Estimate height/width ratio of cover artwork in png_bytes, stripping both white and black letterbox bars."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            bbox = _content_bbox_stripping_letterbox(im)
            if not bbox:
                return None

            cw = bbox[2] - bbox[0]
            ch = bbox[3] - bbox[1]
            if cw > 100 and ch > 100:
                return max(0.65, min(1.55, ch / cw))
    except Exception:
        pass
    return None


def measure_cover_aspect_ratio_from_page(page, png_bytes: bytes | None = None) -> tuple[float | None, str]:
    """Measure the publisher's true cover aspect ratio (height/width) from DOM <img> first, then pixel bbox."""
    js_find_cover_img = """() => {
        let best = null;
        let bestArea = 0;
        for (const img of document.querySelectorAll('img')) {
            const nw = img.naturalWidth || 0;
            const nh = img.naturalHeight || 0;
            if (nw < 180 || nh < 180) continue;
            const rect = img.getBoundingClientRect();
            if (rect.width < 120 || rect.height < 120) continue;
            const area = rect.width * rect.height;
            if (area > bestArea) {
                bestArea = area;
                best = { nw, nh };
            }
        }
        return best;
    }"""
    best_img = None
    best_area = 0
    for scope in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
        try:
            info = scope.evaluate(js_find_cover_img)
            if info and info.get("nw", 0) >= 180 and info.get("nh", 0) >= 180:
                area = info["nw"] * info["nh"]
                if area > best_area:
                    best_area = area
                    best_img = info
        except Exception:
            continue
    if best_img is not None:
        nw, nh = best_img["nw"], best_img["nh"]
        raw_ratio = nh / nw
        if 0.55 <= raw_ratio <= 1.85:
            return max(0.65, min(1.55, raw_ratio)), f"img {nw}x{nh}"
    if png_bytes:
        ratio = measure_cover_aspect_ratio(png_bytes)
        if ratio is not None:
            return ratio, "bbox"
    return None, "none"



def capture_page_as_pdf_bytes(page, cdp_session=None) -> bytes:
    """Capture the current Playwright page as PDF bytes via printToPDF or screenshot fallback."""
    fixed_bytes = capture_fixed_layout_pf_pages(page, cdp_session=cdp_session)
    if fixed_bytes is not None:
        return fixed_bytes

    cleanup_info = inject_print_cleanup(page)
    fixed_layout = bool(cleanup_info.get("fixedLayout"))
    cdp_params = (
        {
            "printBackground": True,
            "preferCSSPageSize": True,
            "marginTop": 0,
            "marginBottom": 0,
            "marginLeft": 0,
            "marginRight": 0,
        }
        if fixed_layout
        else {
            "printBackground": True,
            "preferCSSPageSize": False,
            "marginTop": 0.5,
            "marginBottom": 0.5,
            "marginLeft": 0.4,
            "marginRight": 0.4,
        }
    )

    reader_mode = is_paginated_reader(page)
    if reader_mode:
        _hide_reader_chrome(page)
    try:
        if cdp_session is not None:
            try:
                import base64

                result = cdp_session.send("Page.printToPDF", cdp_params)
                data = base64.b64decode(result["data"])
                if is_pdf_bytes(data):
                    return strip_trailing_blank_pages(data) if fixed_layout else data
            except Exception:
                pass

        try:
            data = page.pdf(
                print_background=True,
                prefer_css_page_size=fixed_layout,
                margin=(
                    {"top": "0", "bottom": "0", "left": "0", "right": "0"}
                    if fixed_layout
                    else {"top": "0.5in", "bottom": "0.5in", "left": "0.4in", "right": "0.4in"}
                ),
            )
            return strip_trailing_blank_pages(data) if fixed_layout else data
        except Exception:
            png_bytes = page.screenshot(full_page=False)
            buf = io.BytesIO()
            with Image.open(io.BytesIO(png_bytes)) as img:
                img.convert("RGB").save(buf, "PDF")
            return buf.getvalue()
    finally:
        if reader_mode:
            _restore_reader_chrome(page)
        remove_print_cleanup(page)


def wait_for_page_ready(page, timeout_ms: int = 12000) -> None:
    """Wait until loading spinners, placeholder text, and pending images finish rendering."""
    try:
        page.wait_for_load_state("networkidle", timeout=min(3000, timeout_ms))
    except Exception:
        pass

    try:
        page.wait_for_function(
            """() => {
                if (document.readyState !== 'complete') return false;

                const bodyText = (document.body?.innerText || '').trim();
                const loadingPhrases = [
                    'hang tight',
                    "this'll take just a sec",
                    "this\u2019ll take just a sec",
                    'loading...',
                    '読み込み中',
                ];
                const lowerText = bodyText.toLowerCase();
                if (loadingPhrases.some(phrase => lowerText.includes(phrase))) {
                    return false;
                }

                const spinnerSelectors = [
                    '[aria-busy="true"]',
                    '.loading',
                    '.spinner',
                    '.loader',
                    '[class*="spinner" i]',
                ];
                for (const sel of spinnerSelectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        const visible = (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            parseFloat(style.opacity || '1') > 0 &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                        if (visible) return false;
                    }
                }

                const contentRoot = document.querySelector('#sbo-rt-content, .sbo-reader-content, [data-testid="book-content"], main article, main');
                const root = contentRoot || document.body;
                const rootText = (root?.innerText || '').trim();
                const hasText = rootText.length > 0;
                const hasCanvas = Array.from((root || document).querySelectorAll('canvas')).some(
                    c => c.width > 0 && c.height > 0
                );
                const imgs = Array.from((root || document).querySelectorAll('img'));
                const pendingImg = imgs.some(img => !img.complete);
                if (pendingImg) return false;
                const hasVisibleImg = imgs.some(img => img.naturalWidth > 10 && img.naturalHeight > 10);

                return hasText || hasCanvas || hasVisibleImg;
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        # Proceed if a non-standard viewer never satisfies every heuristic within timeout
        pass

    # Brief settle window for fonts and layout painting
    page.wait_for_timeout(500)


def check_access_denied(page, response_status: int | None) -> None:
    """Raise a clear error if the loaded page is an Access Denied / 403 block."""
    try:
        title = (page.title() or "").strip().lower()
        body_text = (page.inner_text("body") or "")[:1000].lower()
    except Exception:
        return

    if response_status in {401, 403} or "access denied" in title or "you don't have permission to access" in body_text:
        raise RuntimeError(
            "アクセスが拒否されました (Access Denied)。"
            "ログインや認証が必要なサイトの場合は、Chrome を `--remote-debugging-port=9222` で起動してログイン済みの状態で再実行してください。"
        )


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Sanitize a book/page title for safe macOS PDF filenames."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned


def extract_book_title_and_date(page) -> tuple[str | None, str | None]:
    """Best-effort extraction of (title, YYYYMMDD) from structured metadata or generic HTML pages."""
    url = page.url or ""
    # 1. Book metadata API
    m_catalog = re.search(r"learning\.oreilly\.com/library/view/[^/]+/(\d+)", url)
    if m_catalog:
        book_id = m_catalog.group(1)
        try:
            meta = page.evaluate(
                """async (bid) => {
                    try {
                        const r = await fetch('/api/v2/metadata/?identifier=' + encodeURIComponent(bid) + '&include_hidden=true');
                        if (r.ok) {
                            const d = await r.json();
                            const item = (d.results || d || [])[0];
                            if (item && item.name) {
                                return { title: item.name, pub: (item.publication_date || '').slice(0, 10) };
                            }
                        }
                    } catch (e) {}
                    try {
                        const r2 = await fetch('/api/v2/epubs/urn:orm:book:' + encodeURIComponent(bid) + '/');
                        if (r2.ok) {
                            const d2 = await r2.json();
                            return { title: d2.title || null, pub: (d2.issued || d2.publication_date || '').slice(0, 10) };
                        }
                    } catch (e) {}
                    return null;
                }""",
                book_id,
            )
            if isinstance(meta, dict) and meta.get("title"):
                pub = re.sub(r"[^0-9]", "", str(meta.get("pub") or ""))
                return str(meta["title"]), (pub[:8] if len(pub) >= 8 else None)
        except Exception:
            pass

    # 2. paginated ebook reader title & generic meta tags
    try:
        info = page.evaluate(
            """() => {
                const isReader = location.hostname.includes('read.amazon.');
                let title = '';
                if (isReader) {
                    const lines = (document.body?.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    for (const line of lines) {
                        if (line === 'Kindle Library' || line.startsWith('Page ') || line.includes('left in ')) continue;
                        if (line.length >= 2) { title = line; break; }
                    }
                }
                if (!title) {
                    title = document.querySelector('meta[property="og:title"]')?.content || document.title || '';
                }
                const pubMeta = (
                    document.querySelector('meta[property="article:published_time"]')?.content ||
                    document.querySelector('meta[name="publication_date"]')?.content ||
                    document.querySelector('meta[name="date"]')?.content ||
                    ''
                );
                return { title, pubMeta };
            }"""
        )
        if isinstance(info, dict):
            title = str(info.get("title") or "").strip()
            if title.lower() in {"kindle", "loading", ""}:
                title = ""
            pub_raw = str(info.get("pubMeta") or "")
            m_date = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", pub_raw)
            pub_str = f"{m_date.group(1)}{int(m_date.group(2)):02d}{int(m_date.group(3)):02d}" if m_date else None
            return (title or None), pub_str
    except Exception:
        pass
    return None, None


def resolve_auto_output_path(base_output: Path, title: str | None, pub_date: str | None) -> Path:
    """Build <dir>/<title>_<YYYYMMDD>.pdf (or <title>.pdf), avoiding collisions."""
    safe_title = sanitize_filename(title or "")
    if not safe_title:
        return base_output
    stem = f"{safe_title}_{pub_date}" if pub_date else safe_title
    candidate = base_output.with_name(f"{stem}.pdf")
    if candidate == base_output:
        return candidate
    if not candidate.exists():
        return candidate
    for idx in range(2, 100):
        alt = base_output.with_name(f"{stem}_{idx}.pdf")
        if not alt.exists():
            return alt
    return base_output


def _is_reader_at_end(position: int | None, total: int | None, percent: int | None) -> bool:
    """Single source of truth for whether Kindle Web Reader has reached the final page
    (handles printed pages, Location-based books like 2472/2478 = 99.8%, and 98-100% tails)."""
    if percent is not None and percent >= 98:
        return True
    if total is not None and total > 0 and position is not None:
        if position >= total - 1:
            return True
        if (position / total) >= 0.98:
            return True
    return False


def reader_at_last_page(capture: ReaderCapture, saved_count: int | None = None) -> bool:
    if capture.total is None:
        return False
    if _is_reader_at_end(capture.position, capture.total, capture.percent):
        return True
    # When Kindle hides the footer near the end (position=None, stale percent),
    # having already captured total - 1 or more pages (e.g. 193/194 after skipping 1 blank page) means the book is complete.
    if saved_count is not None and saved_count >= capture.total - 1:
        return True
    return False


def reader_gap_candidates(captures: list[ReaderCapture]) -> list[tuple[int, ReaderCapture, ReaderCapture]]:
    return [
        (ordinal, left, right)
        for ordinal, (left, right) in enumerate(zip(captures, captures[1:]), 1)
        if left.total is not None and left.total == right.total
        and left.position is not None and right.position is not None
        and right.position > left.position + 1
    ]


def reader_frame_matches(page, saved: ReaderCapture) -> bool:
    if not saved.image_hash or get_reader_position(page) != ("page", saved.position) or is_reader_oops(page):
        return False
    _hide_reader_chrome(page)
    try:
        box = _find_reader_page_box(page)
        if box is None:
            return False
        frame = _take_screenshot_resilient(page, clip=box, timeout_ms=12000)
    finally:
        _restore_reader_chrome(page)
    return (
        hashlib.sha256(frame).hexdigest() == saved.image_hash
        or matches_saved_reader_image(frame, saved.pdf, same_printed_page=True)
    )


def follow_reader_gap(page, start: ReaderCapture, end: ReaderCapture, key: str, sign: int) -> list[bytes] | None:
    """Follow one direction between two independently matched images."""
    if start.position is None or end.position is None:
        return None

    def advance() -> bool:
        source = page.evaluate(READER_PAGE_SOURCE)
        page.keyboard.press(key)
        if not wait_for_reader_turn(page, source):
            return False
        page.wait_for_timeout(280)
        return not is_reader_oops(page)

    current = get_reader_position(page)
    if not reader_frame_matches(page, start):
        if not current or current[0] != "page" or not 1 <= (start.position - current[1]) * sign <= 5:
            target = max(1, min(start.total or start.position, start.position - sign))
            if not seek_reader_position(page, target, True, start.total):
                return None
        for _ in range(8):
            if reader_frame_matches(page, start):
                break
            marker = get_reader_position(page)
            if not marker or marker[0] != "page" or (marker[1] - start.position) * sign > 0:
                return None
            if not advance():
                return None
        else:
            return None

    frames = []
    previous = start.position
    seen = {start.image_hash}
    for _ in range(min(40, abs(end.position - start.position) + 6)):
        if not advance():
            return None
        if reader_frame_matches(page, end):
            return frames if sign > 0 else frames[::-1]
        marker = get_reader_position(page)
        if not marker or marker[0] != "page" or not (
            (marker[1] - previous) * sign >= 0 and (end.position - marker[1]) * sign >= 0
        ):
            return None
        screenshot = capture_reader_page_as_pdf_bytes(page)
        if screenshot is None or is_reader_oops(page):
            return None
        pdf, png = screenshot
        digest = hashlib.sha256(png).hexdigest()
        if digest in seen:
            return None
        seen.add(digest)
        frames.append(pdf)
        previous = marker[1]
    return None


def verify_reader_gap(page, left: ReaderCapture, right: ReaderCapture, turn_key: str) -> bytes | None:
    """Verify both saved images and return the PDF pages seen between them."""
    if not left.image_hash or not right.image_hash:
        return None
    frames = follow_reader_gap(page, left, right, turn_key, 1)
    if frames is None and not is_reader_oops(page):
        reverse_key = "ArrowRight" if turn_key == "ArrowLeft" else "ArrowLeft"
        frames = follow_reader_gap(page, right, left, reverse_key, -1)
    if frames is None:
        return None
    if not frames:
        return b""
    writer = PdfWriter()
    for pdf in frames:
        writer.append(io.BytesIO(pdf))
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def verify_merged_reader_pdf(pdfs: list[bytes], output: Path, count: int) -> None:
    """Open the merged PDF independently and compare every page in order."""
    import pymupdf

    def fingerprint(doc, page) -> bytes:
        digests = [
            hashlib.sha256(doc.extract_image(item[0])["image"]).digest()
            for item in page.get_images()
        ]
        return hashlib.sha256(
            repr(tuple(page.rect)).encode() + page.read_contents()
            + page.get_text().encode() + b"".join(digests)
        ).digest()

    with pymupdf.open(output) as merged:
        if len(merged) != count:
            raise RuntimeError("PDFの最終確認でページ数が一致しませんでした。")
        ordinal = 0
        for blob in pdfs:
            with pymupdf.open(stream=blob, filetype="pdf") as original:
                for page in original:
                    if ordinal >= len(merged) or fingerprint(original, page) != fingerprint(merged, merged[ordinal]):
                        raise RuntimeError(f"PDFの最終確認で {ordinal + 1} ページ目が一致しませんでした。")
                    ordinal += 1
        if ordinal != len(merged):
            raise RuntimeError("PDFの最終確認でページ数が一致しませんでした。")
    log(f"[Verify] Final PDF reopened and all {count} pages matched the saved order.")


def _get_cdp_target_id(context, page) -> str | None:
    """Return the Chrome CDP targetId for a page so it can be closed even if Playwright is interrupted."""
    try:
        session = context.new_cdp_session(page)
        try:
            info = session.send("Target.getTargetInfo")
            return info.get("targetInfo", {}).get("targetId")
        finally:
            try:
                session.detach()
            except Exception:
                pass
    except Exception:
        return None


def _close_page_safely(page, cdp_url: str | None = None, target_id: str | None = None) -> None:
    """Close a Playwright page, falling back to Chrome CDP HTTP /json/close when greenlet state is broken."""
    closed = False
    if page is not None:
        try:
            if not page.is_closed():
                page.close()
            closed = True
        except Exception:
            pass
    if not closed and cdp_url and target_id:
        try:
            requests.get(f"{cdp_url.rstrip('/')}/json/close/{target_id}", timeout=2)
        except Exception:
            pass


def detect_reader_reflowable(page) -> bool | None:
    """Detect whether the open Kindle Web Reader book is reflowable (font size / Aa settings enabled)
    versus fixed-layout (comics, scores, print-replica books where Aa settings are absent or disabled)."""
    try:
        result = page.evaluate(
            r"""() => {
                const docs = [document];
                for (const frame of document.querySelectorAll('iframe')) {
                    try {
                        if (frame.contentDocument) docs.push(frame.contentDocument);
                    } catch (_) {}
                }
                let sawToolbar = false;
                let sawEnabledAa = false;
                let sawDisabledAa = false;
                for (const doc of docs) {
                    if (doc.querySelector('#kr-renderer, #top-menu-bar, [aria-label*="Table of Contents" i], [aria-label*="目次"]')) {
                        sawToolbar = true;
                    }
                    const nodes = Array.from(doc.querySelectorAll('button, [role="button"], ion-button'));
                    for (const el of nodes) {
                        const label = (
                            (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('title') || '') + ' ' +
                            (el.getAttribute('data-testid') || '') + ' ' +
                            (el.id || '')
                        ).trim();
                        const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
                        const isAa = (
                            /^Aa$/i.test(text) ||
                            /(?:top_menu_reader_settings|reader\s*settings|font|フォント|文字サイズ)/i.test(label)
                        );
                        if (!isAa) continue;
                        sawToolbar = true;
                        const style = window.getComputedStyle(el);
                        const hidden = style.display === 'none' || style.visibility === 'hidden';
                        const disabled = Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true';
                        if (!hidden && !disabled) {
                            sawEnabledAa = true;
                        } else {
                            sawDisabledAa = true;
                        }
                    }
                }
                if (sawEnabledAa) return true;
                if (sawDisabledAa || sawToolbar) return false;
                return null;
            }"""
        )
        return result if isinstance(result, bool) else None
    except Exception:
        return None


def _new_background_page(browser, context):
    """Create an active tab inside Chrome's window so Chromium does not throttle its compositor/screenshots
    (macOS foreground focus is kept undisturbed by launching Chrome with `open -g` and omitting bring_to_front)."""
    return context.new_page()


def finish_reader_at_end(
    start_url: str, proposed_output: Path, turn_key: str,
    checkpoint: ReaderCheckpoint, captures: list[ReaderCapture],
    result_details: dict[str, int] | None,
    is_reflowable: bool | None = None,
) -> tuple[int, Path]:
    """Check potential gaps within the saved sequence and publish a single PDF."""
    gaps = reader_gap_candidates(captures)
    verified = checkpoint.verified_gaps()
    todo = [(i, left, right) for i, left, right in gaps if i not in verified]
    unchecked = len(todo)
    output = checkpoint.final_path(proposed_output)
    log(f"READER_UNVERIFIED_GAPS={unchecked}")
    if todo:
        log(f"[Reader] Last page reached. Checking {len(todo)} intervals for missing views.")
        try:
            from playwright.sync_api import sync_playwright

            endpoint = resolve_cdp_url()
            if endpoint is None:
                raise RuntimeError("The ebook reader browser session is not available")
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(endpoint)
                context = browser.contexts[0]
                page = _new_background_page(browser, context)
                page_target_id = _get_cdp_target_id(context, page)
                try:
                    page.set_viewport_size({"width": _infer_saved_viewport_width(captures), "height": captures[-1].viewport_height or READER_DEFAULT_HEIGHT})
                    page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                    if not _wait_for_reader_reader(page):
                        raise RuntimeError("The book did not reopen")
                    for index, (ordinal, left, right) in enumerate(todo, 1):
                        log(f"[Reader] Checking interval {index}/{len(todo)} (Reader {left.position}–{right.position}).")
                        try:
                            repaired = verify_reader_gap(page, left, right, turn_key)
                        except Exception as exc:
                            log(f"[Reader] Interval {left.position}–{right.position}: {type(exc).__name__}")
                            repaired = None
                        if repaired is None:
                            log(f"[Reader] Could not verify the saved views for {left.position}–{right.position}.")
                            if is_reader_oops(page):
                                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                                if not _wait_for_reader_reader(page):
                                    break
                        else:
                            checkpoint.save_verified_gap(ordinal, repaired)
                            verified[ordinal] = repaired
                            unchecked -= 1
                            log(f"READER_UNVERIFIED_GAPS={unchecked}")
                finally:
                    _close_page_safely(page, endpoint, page_target_id)
        except Exception as exc:
            log(f"[Reader] Some intervals could not be checked: {exc}")

    pdfs = []
    for ordinal, capture in enumerate(captures, 1):
        pdfs.append(capture.pdf)
        if verified.get(ordinal):
            pdfs.append(verified[ordinal])
    pdfs = normalize_reader_pages_to_cover_size(pdfs, is_reflowable=is_reflowable)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        staged = Path(folder) / output.name
        written = merge_pdf_bytes_list(pdfs, staged)
        verify_merged_reader_pdf(pdfs, staged, written)
        staged.replace(output)
    log(f"[Reader] Finalized {written} PDF pages; {unchecked} intervals could not be verified.")
    if result_details is not None:
        result_details.update({
            "expectedPages": captures[-1].total,
            "lastPrintedPage": captures[-1].position,
            "unverifiedGaps": unchecked,
            "verifiedPages": written,
        })
    if unchecked == 0:
        checkpoint.remove()
    return written, output


def fetch_via_browser(
    start_url: str,
    output_path: Path,
    max_pages: int,
    timeout: int,
    direction: str = "right",
    auto_name: bool = False,
    reader_checkpoint: ReaderCheckpoint | None = None,
    result_details: dict[str, int] | None = None,
    aspect_preset: str = "auto",
    quality: str = "high",
) -> tuple[int, Path]:
    """Use CDP (logged-in Chrome) or headless Chromium to paginate with ArrowRight/ArrowLeft and capture PDFs."""
    scale_factor = 2 if quality == "high" else 1
    turn_key = "ArrowLeft" if direction == "left" else "ArrowRight"
    saved_captures = reader_checkpoint.load() if reader_checkpoint and reader_checkpoint.path.exists() else []
    if saved_captures and reader_at_last_page(saved_captures[-1], saved_count=len(saved_captures)):
        return finish_reader_at_end(start_url, output_path, turn_key, reader_checkpoint, saved_captures, result_details)

    from playwright.sync_api import sync_playwright

    captured_pdfs: list[bytes] = []
    seen_pdf_urls: set[str] = set()

    with sync_playwright() as p:
        active_cdp_url = resolve_cdp_url()
        use_cdp = active_cdp_url is not None
        if use_cdp and active_cdp_url is not None:
            log(f"[Browser] Connecting to logged-in Chrome via CDP ({active_cdp_url})...")
            browser = p.chromium.connect_over_cdp(active_cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            is_reader_url = "read.amazon." in start_url.lower()
            page = None if is_reader_url else next((pg for pg in context.pages if pg.url == start_url), None)
            created_page = False
            reused_dedicated_tab = False
            page_target_id: str | None = None
            if page is None:
                # Reuse an existing tab inside dedicated Chrome so macOS never brings Chrome to the foreground
                # (Target.createTarget activates Chrome on macOS, whereas navigating an existing tab does not).
                existing_pages = [pg for pg in list(context.pages) if not pg.is_closed()]
                if existing_pages:
                    reusable = next(
                        (pg for pg in existing_pages if (pg.url or "").lower().startswith(("about:blank", "chrome://newtab"))),
                        None,
                    )
                    if reusable is None and is_reader_url:
                        reusable = next(
                            (pg for pg in existing_pages if "read.amazon." in (pg.url or "").lower()),
                            None,
                        )
                    page = reusable or existing_pages[-1]
                    reused_dedicated_tab = True
                else:
                    page = _new_background_page(browser, context)
                    created_page = True
                if is_reader_url:
                    asin_match = re.search(r"(?:[?&]asin=|/manga/)([A-Z0-9]+)", start_url, re.I)
                    target_asin = asin_match.group(1).upper() if asin_match else None
                    if target_asin:
                        for other in list(context.pages):
                            if other == page:
                                continue
                            try:
                                other_url = (other.url or "").upper()
                                if "READ.AMAZON." in other_url and target_asin in other_url:
                                    other_tid = _get_cdp_target_id(context, other)
                                    _close_page_safely(other, active_cdp_url, other_tid)
                                    log(f"[Reader] Closed duplicate open tab for ASIN {target_asin} to prevent Kindle reader conflict.")
                            except Exception:
                                pass
                page_target_id = _get_cdp_target_id(context, page)
        else:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 1600},
            )
            page = context.new_page()
            created_page = True
            reused_dedicated_tab = False
            page_target_id = None
        owns_viewport = created_page or reused_dedicated_tab

        cdp_session = None
        try:
            try:
                cdp_session = context.new_cdp_session(page)
            except Exception:
                cdp_session = None

            def handle_response(response) -> None:
                try:
                    ctype = (response.headers.get("content-type") or "").lower()
                    if "application/pdf" in ctype and response.url not in seen_pdf_urls:
                        body = response.body()
                        if is_pdf_bytes(body):
                            seen_pdf_urls.add(response.url)
                            captured_pdfs.append(body)
                            log(f"[Browser] Intercepted PDF response: {response.url} ({len(body)} bytes)")
                except Exception:
                    pass

            page.on("response", handle_response)
            if owns_viewport and "read.amazon." in start_url.lower():
                try:
                    page.set_viewport_size({"width": READER_VIEWPORT_WIDTH, "height": READER_DEFAULT_HEIGHT})
                except Exception:
                    pass
            response_status: int | None = None
            if owns_viewport or page.url != start_url:
                log(f"[Browser] Opening {start_url} ...")
                nav_resp = page.goto(start_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                response_status = nav_resp.status if nav_resp else None
            else:
                log(f"[Browser] Using already-open tab: {page.url}")

            log("[Browser] Waiting for page render...")
            wait_for_page_ready(page, timeout_ms=12000)
            check_access_denied(page, response_status)
            resolved_output = output_path
            reader_mode = is_paginated_reader(page)
            detected_title, detected_pub = None, None
            reader_error_at_open = reader_mode and is_reader_oops(page)
            if (auto_name or reader_mode) and not reader_error_at_open:
                detected_title, detected_pub = extract_book_title_and_date(page)
                if detected_title and result_details is not None:
                    result_details["bookTitle"] = detected_title
            if auto_name:
                resolved_output = resolve_auto_output_path(output_path, detected_title, detected_pub)
                if resolved_output != output_path:
                    log(f"[AutoName] Output filename -> {resolved_output.name}")
            book_title = detected_title if reader_mode else None
            reader_is_reflowable: bool | None = None
            if reader_mode and not reader_error_at_open:
                reader_is_reflowable = detect_reader_reflowable(page)
                if reader_is_reflowable is True:
                    log("[Reader] Layout type: reflowable (Aa font settings available -> adding vertical reading margins if needed).")
                elif reader_is_reflowable is False:
                    log("[Reader] Layout type: fixed-layout (no Aa font settings -> preserving exact edge-to-edge layout).")
            saved_pages = saved_captures
            restored_skipped_steps = 0
            if saved_pages:
                log(f"[Reader] Loaded {len(saved_pages)} saved page(s); resuming capture.")
                if saved_pages[-1].total is not None:
                    log(f"READER_PROGRESS={len(saved_pages)}/{saved_pages[-1].total}")
                if reader_mode and saved_pages[-1].viewport_height is not None:
                    _apply_reader_viewport(page, _infer_saved_viewport_width(saved_pages), saved_pages[-1].viewport_height, scale_factor=scale_factor, cdp_session=cdp_session)
                elif not reader_mode and reader_checkpoint is not None:
                    resume_url, restored_skipped_steps = reader_checkpoint.load_resume_state()
                    if resume_url and resume_url != page.url:
                        log(f"[Browser] Jumping to saved resume URL: {resume_url}")
                        page.goto(resume_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                        wait_for_page_ready(page, timeout_ms=15000)
                        page.keyboard.press(turn_key)
                        page.wait_for_timeout(800)
                        wait_for_page_ready(page, timeout_ms=10000)
            elif reader_mode:
                dismiss_reader_dialogs(page)
                if owns_viewport:
                    try:
                        init_w, init_h = READER_ASPECT_PRESETS.get(aspect_preset, (READER_VIEWPORT_WIDTH, READER_DEFAULT_HEIGHT))
                        _apply_reader_viewport(page, init_w, init_h, scale_factor=scale_factor, cdp_session=cdp_session)
                        page.wait_for_timeout(800)
                    except Exception:
                        pass

            # 1. If a PDF was fetched via XHR/embed during load, merge and return
            if captured_pdfs and not saved_pages:
                return merge_pdf_bytes_list(captured_pdfs, resolved_output), resolved_output

            # 2. Check DOM for JS-injected PDF links / iframes
            html = page.content()
            dom_pdf_links = extract_pdf_links_from_html(html, page.url)
            if dom_pdf_links and not saved_pages:
                session = create_session()
                blobs: list[bytes] = []
                for link in dom_pdf_links:
                    blob = download_if_pdf(session, link, timeout=timeout)
                    if blob:
                        blobs.append(blob)
                if blobs:
                    return merge_pdf_bytes_list(blobs, resolved_output), resolved_output

            # 3. Paginate with ArrowRight / ArrowLeft and capture each page via PDF command
            if direction == "left":
                arrow_button_selectors = [
                    "#kr-chevron-left:not([disabled])",
                    "button:has-text('←'):not([disabled])",
                    "a:has-text('←')",
                    "button:has-text('‹'):not([disabled])",
                    "a:has-text('‹')",
                    "[aria-label*='Next' i]:not([disabled])",
                    "[title*='Next' i]:not([disabled])",
                    "button:has-text('次へ'):not([disabled])",
                    "a:has-text('次へ')",
                    "[rel='next']",
                ]
            else:
                arrow_button_selectors = [
                    "#kr-chevron-right:not([disabled])",
                    "button:has-text('→'):not([disabled])",
                    "a:has-text('→')",
                    "button:has-text('›'):not([disabled])",
                    "a:has-text('›')",
                    "[aria-label*='Next' i]:not([disabled])",
                    "[title*='Next' i]:not([disabled])",
                    "button:has-text('次へ'):not([disabled])",
                    "a:has-text('次へ')",
                    "[rel='next']",
                ]

            collected_page_pdfs = [capture.pdf for capture in saved_pages]
            skipped_blank_steps = restored_skipped_steps
            consecutive_blank_skips = 0
            start_asin = _extract_reader_asin(start_url)
            prev_was_near_end = False
            prev_hash: str | None = None
            prev_pdf_hash: str | None = None
            recent_pdf_hashes: list[str] = []
            last_reader_page: int | None = None
            expected_total_pages: int | None = None
            last_reader_percent: int | None = None
            last_reader_image_hash: str | None = None
            oreilly_last_filename: str | None = None
            recovery_attempts = 0
            needs_recovery = bool(saved_pages) and reader_mode
            clean_single_turn = False
            if saved_pages:
                last_capture = saved_pages[-1]
                prev_pdf_hash = pdf_content_signature(last_capture.pdf)
                last_reader_page = last_capture.position
                last_reader_image_hash = last_capture.image_hash
                expected_total_pages = last_capture.total
                last_reader_percent = last_capture.percent
            if len(saved_pages) > max_pages:
                raise ValueError(f"最大ページ数を取得済みの {len(saved_pages)} ページ以上に設定してください。")

            if reader_mode and hasattr(signal, "SIGALRM"):
                signal.signal(signal.SIGALRM, _handle_step_timeout)

            while len(collected_page_pdfs) < max_pages:
                page_idx = len(collected_page_pdfs) + 1
                reader_png = None
                if reader_mode and hasattr(signal, "SIGALRM"):
                    signal.alarm(60)
                if reader_mode:
                    dismiss_reader_dialogs(page)
                    if page_idx > 1 and not needs_recovery and not is_reader_oops(page):
                        upsell_reason = is_reader_upsell_or_next_book(page, start_asin, book_title)
                        if upsell_reason:
                            log(f"[Reader] {upsell_reason}; stopping pagination cleanly.")
                            break
                        if prev_was_near_end:
                            cur_m = get_reader_position(page)
                            cur_t = get_reader_total_pages(page)
                            cur_p = get_reader_progress_percent(page)
                            if (
                                (cur_m is not None and cur_m[1] <= 5)
                                or (cur_p is not None and cur_p <= 10)
                                or (expected_total_pages is not None and cur_t is not None and cur_t != expected_total_pages and cur_t < expected_total_pages)
                            ):
                                log(f"[Reader] Reader position reset after end of book (pos={cur_m}, total={cur_t}, pct={cur_p}); next-volume sample detected — stopping cleanly.")
                                break
                    if needs_recovery or is_reader_oops(page):
                        if _is_reader_at_end(last_reader_page, expected_total_pages, last_reader_percent):
                            break
                        if recovery_attempts >= 5:
                            raise IncompleteReaderCapture(
                                f"リーダーのエラーから復旧できませんでした（取得済み: {page_idx - 1} ページ）。",
                                page_idx - 1, expected_total_pages,
                            )
                        recovery_attempts += 1
                        if recovery_attempts > 1:
                            cooldown_ms = min(8000, recovery_attempts * 2000)
                            log(f"[Reader] Cooling down {cooldown_ms / 1000:.0f}s before retry {recovery_attempts}/5...")
                            if not page.is_closed():
                                page.wait_for_timeout(cooldown_ms)
                        log(f"[Reader] Recovering before capture {page_idx} ({recovery_attempts}/5)...")
                        recovery_options = dict(
                            captured_count=page_idx - 1,
                            last_image_hash=last_reader_image_hash,
                            toc_offset=1 if recovery_attempts >= 3 else None,
                            book_title=book_title,
                            use_location=recovery_attempts <= 4,
                            expected_total_pages=expected_total_pages,
                            last_pdf=collected_page_pdfs[-1] if collected_page_pdfs else None,
                        )
                        if recovery_attempts == 1:
                            recovered = recover_reader_oops(
                                page, start_url, last_reader_page, turn_key, **recovery_options,
                            )
                        else:
                            log("[Reader] Trying a new tab with the current browser session...")
                            recovered = False
                            candidate = None
                            candidate_target_id: str | None = None
                            try:
                                candidate = _new_background_page(browser, context) if use_cdp else context.new_page()
                                candidate_target_id = _get_cdp_target_id(context, candidate) if use_cdp else None
                                if page.viewport_size:
                                    candidate.set_viewport_size(page.viewport_size)
                                elif created_page:
                                    candidate.set_viewport_size({"width": READER_VIEWPORT_WIDTH, "height": READER_DEFAULT_HEIGHT})
                                recovered = recover_reader_oops(
                                    candidate, start_url, last_reader_page, turn_key,
                                    fresh_tab=True, **recovery_options,
                                )
                            except Exception as exc:
                                log(f"[Reader] New-tab recovery failed ({type(exc).__name__}).")
                            finally:
                                if candidate is not None and not recovered:
                                    _close_page_safely(candidate, active_cdp_url, candidate_target_id)
                            if recovered:
                                previous_page, previous_owned, previous_target_id = page, created_page, page_target_id
                                page, created_page, page_target_id = candidate, True, candidate_target_id
                                page.on("response", handle_response)
                                cdp_session = None
                                try:
                                    cdp_session = context.new_cdp_session(page)
                                except Exception:
                                    pass
                                if previous_owned:
                                    _close_page_safely(previous_page, active_cdp_url, previous_target_id)
                                log("[Reader] Switched capture to the verified new tab.")
                        needs_recovery = not recovered
                        if recovered and book_title is None:
                            book_title, published = extract_book_title_and_date(page)
                            if auto_name and resolved_output == output_path and book_title:
                                resolved_output = resolve_auto_output_path(output_path, book_title, published)
                        if needs_recovery and not page.is_closed():
                            page.wait_for_timeout(1000)
                        continue
                    if page_idx == 1 and recovery_attempts == 0:
                        _wait_for_reader_reader(page, timeout_ms=15000)
                        dismiss_reader_dialogs(page)
                        if is_reader_oops(page):
                            continue
                        for rewind_try in range(2):
                            # Always run rewind_reader_to_start on the first attempt even if already at Page 1 / 0%,
                            # because some Kindle books place the Cover before Page 1 and only make it reachable via TOC.
                            if rewind_try > 0:
                                initial_pos = get_reader_position(page)
                                if not is_reader_not_at_start(page) and (initial_pos is None or initial_pos[1] <= 1):
                                    break
                            rewind_reader_to_start(page, turn_key)
                            dismiss_reader_dialogs(page)
                            if is_reader_oops(page):
                                break
                            after_pos = get_reader_position(page)
                            after_pct = get_reader_progress_percent(page)
                            at_start = (
                                (after_pct is not None and after_pct == 0)
                                or (after_pos is None and (after_pct is None or after_pct <= 1))
                                or (after_pos is not None and after_pos[0] == "page" and after_pos[1] <= 1)
                                or (after_pos is not None and after_pos[0] == "location" and after_pos[1] <= 25 and (after_pct is None or after_pct <= 1))
                            )
                            if at_start:
                                break
                            log(f"[Reader] Still not at cover after rewind {rewind_try + 1}/2 (pos={after_pos}, pct={after_pct}).")
                        if is_reader_oops(page):
                            continue
                        verify_pos = get_reader_position(page)
                        verify_pct = get_reader_progress_percent(page)
                        still_far_from_start = (
                            (verify_pct is not None and verify_pct >= 5)
                            or (verify_pos is not None and verify_pos[0] == "page" and verify_pos[1] > 2 and (verify_pct is None or verify_pct > 0))
                        )
                        if still_far_from_start:
                            raise RuntimeError(
                                f"表紙（1ページ目）まで戻れませんでした（現在位置: {verify_pos[1] if verify_pos else '?'} / {verify_pct if verify_pct is not None else '?'}%）。再ダウンロードしてください。"
                            )
                        detected_key = detect_reader_turn_direction(page, turn_key)
                        if detected_key != turn_key:
                            turn_key = detected_key
                            detected_dir = "left" if turn_key == "ArrowLeft" else "right"
                            log(f"DETECTED_DIRECTION={detected_dir}")
                            if result_details is not None:
                                result_details["detectedDirection"] = detected_dir
                else:
                    wait_for_page_ready(page, timeout_ms=15000)
                    for _ in range(3):
                        try:
                            body_low = (page.inner_text("body") or "").lower()
                        except Exception:
                            body_low = ""
                        if not any(ph in body_low for ph in ("hang tight", "this'll take just a sec", "this\u2019ll take just a sec", "loading...")):
                            break
                        log(f"[Browser] Page {page_idx} still loading; waiting 3s...")
                        page.wait_for_timeout(3000)
                        wait_for_page_ready(page, timeout_ms=10000)
                    if expected_total_pages is None and "learning.oreilly.com" in (page.url or ""):
                        m_book = re.search(r"learning\.oreilly\.com/library/view/[^/]+/(\d+)", page.url or "")
                        if m_book:
                            try:
                                spine_info = page.evaluate(
                                    """async ([bid, startUrl]) => {
                                        try {
                                            const r = await fetch('/api/v2/epubs/urn:orm:book:' + encodeURIComponent(bid) + '/');
                                            if (!r.ok) return null;
                                            const d = await r.json();
                                            if (typeof d.spine === 'string') {
                                                const sep = d.spine.includes('?') ? '&' : '?';
                                                const sr = await fetch(d.spine + sep + 'limit=1000');
                                                if (sr.ok) {
                                                    const sd = await sr.json();
                                                    const count = typeof sd.count === 'number' ? sd.count : null;
                                                    const results = Array.isArray(sd.results) ? sd.results : [];
                                                    const normName = (u) => {
                                                        if (!u) return '';
                                                        const clean = String(u).split('#')[0].split('?')[0].replace(/\\/+$/, '');
                                                        return clean.split('/').pop() || '';
                                                    };
                                                    const startName = normName(startUrl);
                                                    let startIndex = 0;
                                                    if (startName && results.length > 0) {
                                                        const idx = results.findIndex((item) => {
                                                            const candidate = item.full_path || item.url || item.href || item.reference_id || '';
                                                            return normName(candidate) === startName;
                                                        });
                                                        if (idx >= 0) startIndex = idx;
                                                    }
                                                    let lastFilename = null;
                                                    if (results.length > 0) {
                                                        const lastItem = results[results.length - 1];
                                                        lastFilename = normName(lastItem.full_path || lastItem.url || lastItem.href || '');
                                                    }
                                                    if (count && count > 0) {
                                                        return {
                                                            total: Math.max(1, count - startIndex),
                                                            rawCount: count,
                                                            startIndex,
                                                            lastFilename,
                                                        };
                                                    }
                                                }
                                            }
                                            if (typeof d.page_count === 'number' && d.page_count > 0) {
                                                return { total: d.page_count, rawCount: d.page_count, startIndex: 0, lastFilename: null };
                                            }
                                        } catch (e) {}
                                        return null;
                                    }""",
                                    [m_book.group(1), start_url],
                                )
                                if isinstance(spine_info, dict) and isinstance(spine_info.get("total"), int) and spine_info["total"] > 0:
                                    expected_total_pages = spine_info["total"]
                                    oreilly_last_filename = spine_info.get("lastFilename") or None
                            except Exception:
                                pass
                png_bytes = _take_screenshot_resilient(page, timeout_ms=15000)
                current_hash = hashlib.sha256(png_bytes).hexdigest()

                if prev_hash == current_hash:
                    if not reader_mode:
                        for retry_idx in range(2):
                            log(f"[Browser] Page {page_idx} unchanged; retrying turn ({retry_idx + 1}/2)...")
                            page.keyboard.press(turn_key)
                            page.wait_for_timeout(1800)
                            wait_for_page_ready(page, timeout_ms=10000)
                            png_retry = _take_screenshot_resilient(page, timeout_ms=15000)
                            retry_hash = hashlib.sha256(png_retry).hexdigest()
                            if retry_hash == prev_hash:
                                for selector in arrow_button_selectors:
                                    locator = page.locator(selector).first
                                    try:
                                        if locator.count() > 0 and locator.is_visible():
                                            locator.click(timeout=3000)
                                            page.wait_for_timeout(1800)
                                            wait_for_page_ready(page, timeout_ms=10000)
                                            break
                                    except Exception:
                                        continue
                                png_retry = _take_screenshot_resilient(page, timeout_ms=15000)
                                retry_hash = hashlib.sha256(png_retry).hexdigest()
                            if retry_hash != prev_hash:
                                png_bytes = png_retry
                                current_hash = retry_hash
                                break
                    if reader_mode:
                        # Retry page turn on slow networks / chapter boundaries before stopping
                        max_turn_retries = 3 if (expected_total_pages and last_reader_page and last_reader_page < expected_total_pages) else 2
                        advanced = False
                        for retry_idx in range(max_turn_retries):
                            log(f"[Reader] Page {page_idx} unchanged; retrying turn ({retry_idx + 1}/{max_turn_retries})...")
                            dismiss_reader_dialogs(page)
                            if is_reader_oops(page):
                                break
                            source = page.evaluate(READER_PAGE_SOURCE)
                            page.keyboard.press(turn_key)
                            if source:
                                wait_for_reader_turn(page, source)
                            else:
                                page.wait_for_timeout(1500)
                            if is_reader_oops(page):
                                break
                            png_retry = _take_screenshot_resilient(page, timeout_ms=15000)
                            retry_hash = hashlib.sha256(png_retry).hexdigest()
                            if retry_hash == prev_hash:
                                for selector in arrow_button_selectors:
                                    locator = page.locator(selector).first
                                    try:
                                        if locator.count() > 0 and locator.is_visible():
                                            locator.click(timeout=3000)
                                            page.wait_for_timeout(1500)
                                            break
                                    except Exception:
                                        continue
                                if is_reader_oops(page):
                                    break
                                png_retry = _take_screenshot_resilient(page, timeout_ms=15000)
                                retry_hash = hashlib.sha256(png_retry).hexdigest()
                            if retry_hash != prev_hash:
                                png_bytes = png_retry
                                current_hash = retry_hash
                                advanced = True
                                break
                        if is_reader_oops(page):
                            continue
                        if not advanced and not _is_reader_at_end(last_reader_page, expected_total_pages, last_reader_percent) and (recovery_attempts or (
                            expected_total_pages is not None and last_reader_page is not None
                            and last_reader_page < expected_total_pages
                        )):
                            needs_recovery = True
                            continue
                    if prev_hash == current_hash:
                        log(f"[Browser] Page {page_idx} identical to previous; stopping pagination.")
                        break

                pdf_count_before = len(captured_pdfs)
                if reader_mode:
                    if _find_reader_page_box(page) is None:
                        _wait_for_reader_reader(page, timeout_ms=8000)
                    if is_reader_oops(page):
                        continue
                    k_res = capture_reader_page_as_pdf_bytes(page, cdp_session=cdp_session, scale_factor=scale_factor)
                    if k_res is not None:
                        page_pdf_bytes, reader_png = k_res
                        if page_idx == 1 and created_page:
                            cover_ratio, ratio_source = measure_cover_aspect_ratio_from_page(page, reader_png)
                            if cover_ratio is not None:
                                log(f"[Reader] Cover aspect ratio: {cover_ratio:.3f} ({ratio_source}), preset={aspect_preset}, quality={quality} ({scale_factor}x)")
                                if cover_ratio < 1.15:
                                    # Keep natural landscape / spread aspect ratio for fixed-shape books (scores, spreads)
                                    target_w = READER_LANDSCAPE_VIEWPORT_WIDTH
                                    target_h = max(900, min(1360, int(round(target_w * cover_ratio))))
                                elif aspect_preset in READER_ASPECT_PRESETS:
                                    target_w, target_h = READER_ASPECT_PRESETS[aspect_preset]
                                else:
                                    # auto: match the publisher's exact cover aspect ratio without extra vertical padding
                                    target_w = READER_VIEWPORT_WIDTH
                                    target_h = max(1000, min(1560, int(round(target_w * cover_ratio))))
                                try:
                                    _apply_reader_viewport(page, target_w, target_h, scale_factor=scale_factor, cdp_session=cdp_session)
                                    page.wait_for_timeout(800)
                                    k_res2 = capture_reader_page_as_pdf_bytes(page, cdp_session=cdp_session, scale_factor=scale_factor)
                                    if k_res2 is not None:
                                        page_pdf_bytes, reader_png = k_res2
                                except Exception:
                                    pass
                    elif last_reader_image_hash is not None:
                        needs_recovery = True
                        continue
                    else:
                        page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                else:
                    page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                if reader_mode and is_reader_oops(page):
                    continue
                if is_pdf_loading_or_error(page_pdf_bytes):
                    for retry in range(4):
                        log(f"[Browser] Page {page_idx} rendered loading/error screen; waiting and re-capturing ({retry + 1}/4)...")
                        page.wait_for_timeout(3000)
                        if retry == 2 and not reader_mode:
                            try:
                                page.reload(wait_until="domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                        wait_for_page_ready(page, timeout_ms=12000)
                        if reader_mode:
                            if is_reader_oops(page):
                                break
                            retry_capture = capture_reader_page_as_pdf_bytes(page, scale_factor=scale_factor)
                            if retry_capture is not None:
                                page_pdf_bytes, reader_png = retry_capture
                            else:
                                continue
                        else:
                            page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                        if not is_pdf_loading_or_error(page_pdf_bytes) and not is_pdf_visually_blank(page_pdf_bytes):
                            break
                    if reader_mode and is_reader_oops(page):
                        continue
                    if is_pdf_loading_or_error(page_pdf_bytes):
                        if reader_mode and last_reader_image_hash is not None:
                            needs_recovery = True
                            continue
                        log(f"[Browser] Page {page_idx} remained on loading/error screen after retries; discarding and stopping.")
                        break
                if reader_mode and is_pdf_loading_spinner(page_pdf_bytes):
                    for spin_retry in range(8):
                        log(f"[Reader] Page {page_idx} showing loading spinner; waiting for manga/page render ({spin_retry + 1}/8)...")
                        page.wait_for_timeout(1200)
                        if is_reader_oops(page):
                            break
                        spin_cap = capture_reader_page_as_pdf_bytes(page, cdp_session=cdp_session, scale_factor=scale_factor)
                        if spin_cap is not None:
                            page_pdf_bytes, reader_png = spin_cap
                        if not is_pdf_loading_spinner(page_pdf_bytes):
                            break
                    if is_reader_oops(page):
                        continue
                    if is_pdf_loading_spinner(page_pdf_bytes):
                        log(f"[Reader] Page {page_idx} remained on loading spinner after 10s; triggering recovery instead of saving spinner.")
                        needs_recovery = True
                        continue
                if is_pdf_visually_blank(page_pdf_bytes):
                    for retry in range(2):
                        log(f"[Browser] Page {page_idx} rendered blank; waiting and retrying ({retry + 1}/2)...")
                        page.wait_for_timeout(1500)
                        wait_for_page_ready(page, timeout_ms=10000)
                        if reader_mode:
                            if is_reader_oops(page):
                                break
                            retry_capture = capture_reader_page_as_pdf_bytes(page, scale_factor=scale_factor)
                            if retry_capture is not None:
                                page_pdf_bytes, reader_png = retry_capture
                            else:
                                continue
                        else:
                            page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                        if not is_pdf_visually_blank(page_pdf_bytes):
                            break
                    if reader_mode and is_reader_oops(page):
                        continue
                    if is_pdf_visually_blank(page_pdf_bytes):
                        if reader_mode and last_reader_image_hash is not None:
                            if recovery_attempts == 0 and consecutive_blank_skips == 0:
                                needs_recovery = True
                                continue
                            if consecutive_blank_skips < 5:
                                consecutive_blank_skips += 1
                                skipped_blank_steps += 1
                                recovery_attempts = 0
                                log(f"[Reader] Page {page_idx} confirmed intentionally blank after reload ({consecutive_blank_skips}/5); advancing to next page.")
                                page.keyboard.press(turn_key)
                                page.wait_for_timeout(900)
                                continue
                            needs_recovery = True
                            continue
                        if page_idx == 1:
                            raise RuntimeError(
                                "ページの描画が空（白紙）のままだったためPDFを作成できませんでした。"
                                "ログイン状態やページ読み込みを確認して再実行してください。"
                            )
                        url_before_skip = page.url
                        page.keyboard.press(turn_key)
                        page.wait_for_timeout(1200)
                        wait_for_page_ready(page, timeout_ms=10000)
                        if page.url != url_before_skip:
                            skipped_blank_steps += 1
                            if reader_checkpoint is not None:
                                reader_checkpoint.save_resume_url(url_before_skip, skipped_blank_steps)
                            log(f"[Browser] Page {page_idx} was intentionally blank ({url_before_skip.rsplit('/', 1)[-1]}); skipping to next page.")
                            continue
                        if expected_total_pages is not None:
                            m_ch = re.search(r"chapter-(\d+)\.html", url_before_skip)
                            if m_ch and int(m_ch.group(1)) >= expected_total_pages:
                                skipped_blank_steps += 1
                                log(f"[Browser] Final chapter ({url_before_skip.rsplit('/', 1)[-1]}) was intentionally blank; completing.")
                                break
                        log(f"[Browser] Page {page_idx} remained blank after retry; stopping pagination.")
                        break
                current_pdf_hash = pdf_content_signature(page_pdf_bytes)
                if prev_pdf_hash == current_pdf_hash:
                    if not reader_mode:
                        for pdf_retry in range(2):
                            log(f"[Browser] Page {page_idx} PDF content unchanged; retrying turn ({pdf_retry + 1}/2)...")
                            page.keyboard.press(turn_key)
                            page.wait_for_timeout(2000)
                            wait_for_page_ready(page, timeout_ms=10000)
                            page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                            current_pdf_hash = pdf_content_signature(page_pdf_bytes)
                            if current_pdf_hash != prev_pdf_hash and not is_pdf_visually_blank(page_pdf_bytes):
                                break
                            current_pdf_hash = prev_pdf_hash
                    if reader_mode:
                        next_chevron = "#kr-chevron-left, [data-testid='chevron-left']" if turn_key == "ArrowLeft" else "#kr-chevron-right, [data-testid='chevron-right']"
                        at_book_end = (
                            (last_reader_percent is not None and last_reader_percent >= 98)
                            or (
                                expected_total_pages is not None
                                and expected_total_pages > 0
                                and last_reader_page is not None
                                and (last_reader_page / expected_total_pages) >= 0.98
                            )
                        )
                        if not at_book_end:
                            try:
                                chev_check = page.locator(next_chevron).first
                                if chev_check.count() == 0 or not chev_check.is_visible():
                                    # When Kindle hides the forward chevron and we are past 90%, we are at the true last page.
                                    if (
                                        (last_reader_percent is not None and last_reader_percent >= 90)
                                        or (expected_total_pages and last_reader_page and (last_reader_page / expected_total_pages) >= 0.90)
                                    ):
                                        at_book_end = True
                            except Exception:
                                pass
                        max_pdf_retries = 1 if at_book_end else 3
                        for pdf_retry in range(max_pdf_retries):
                            log(f"[Reader] Page {page_idx} PDF content unchanged; retrying turn ({pdf_retry + 1}/{max_pdf_retries})...")
                            dismiss_reader_dialogs(page)
                            source = page.evaluate(READER_PAGE_SOURCE)
                            if pdf_retry == 0:
                                # 1st retry: press turn_key, and if unchanged try clicking the next chevron directly.
                                page.keyboard.press(turn_key)
                                turned = wait_for_reader_turn(page, source) if source else False
                                if not turned:
                                    try:
                                        chev = page.locator(next_chevron).first
                                        if chev.count() > 0 and chev.is_visible():
                                            chev.click(force=True, timeout=1500)
                                            page.wait_for_timeout(600)
                                    except Exception:
                                        pass
                            elif (
                                pdf_retry == 1
                                and not at_book_end
                                and last_reader_page is not None
                                and expected_total_pages is not None
                                and last_reader_page < expected_total_pages
                            ):
                                # 2nd retry: jump directly to next page/location number via Go to Page / Location.
                                log(f"[Reader] Turn stuck at {last_reader_page}/{expected_total_pages}; trying direct jump to {last_reader_page + 1}...")
                                try:
                                    _reveal_reader_toolbar(page)
                                    seek_reader_position(page, last_reader_page + 1, target_is_printed_page=True, expected_total_pages=expected_total_pages)
                                    page.wait_for_timeout(600)
                                except Exception:
                                    try:
                                        page.keyboard.press("Escape")
                                    except Exception:
                                        pass
                            else:
                                # 3rd retry: try TOC next chapter if still well before the end, else stop cleanly.
                                if (
                                    not at_book_end
                                    and expected_total_pages is not None
                                    and last_reader_page is not None
                                    and last_reader_page < expected_total_pages
                                ):
                                    log("[Reader] Direct jump did not advance; trying next chapter via Table of Contents...")
                                    try:
                                        _reveal_reader_toolbar(page)
                                        jump_to_reader_chapter(page, 1)
                                        page.wait_for_timeout(600)
                                    except Exception:
                                        try:
                                            page.keyboard.press("Escape")
                                        except Exception:
                                            pass
                                else:
                                    page.keyboard.press(turn_key)
                                    page.wait_for_timeout(1200)
                            if is_reader_oops(page):
                                break
                            k_res_retry = capture_reader_page_as_pdf_bytes(page, scale_factor=scale_factor)
                            if k_res_retry is not None:
                                page_pdf_bytes, reader_png = k_res_retry
                            elif last_reader_image_hash is None:
                                page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                            else:
                                continue
                            current_pdf_hash = pdf_content_signature(page_pdf_bytes)
                            if current_pdf_hash != prev_pdf_hash and not is_pdf_visually_blank(page_pdf_bytes):
                                break
                            current_pdf_hash = prev_pdf_hash
                    if reader_mode and is_reader_oops(page):
                        continue
                    if prev_pdf_hash == current_pdf_hash:
                        if reader_mode and not _is_reader_at_end(last_reader_page, expected_total_pages, last_reader_percent) and (recovery_attempts or (
                            expected_total_pages is not None and last_reader_page is not None
                            and last_reader_page < expected_total_pages
                        )):
                            needs_recovery = True
                            continue
                        if reader_mode:
                            try:
                                stop_snippet = (page.inner_text("body") or "").replace("\n", " | ")[:200]
                                log(f"[Reader] Stop context: {stop_snippet}")
                            except Exception:
                                pass
                        log(f"[Browser] Page {page_idx} PDF content identical to previous; stopping pagination.")
                        break
                if reader_mode and is_reader_oops(page):
                    continue
                if reader_mode and current_pdf_hash in recent_pdf_hashes:
                    log(f"[Reader] Detected repeated page cycle at page {page_idx} (already captured in recent pages); stopping pagination cleanly.")
                    break
                prev_hash = current_hash
                prev_pdf_hash = current_pdf_hash
                recent_pdf_hashes.append(current_pdf_hash)
                if len(recent_pdf_hashes) > 10:
                    recent_pdf_hashes = recent_pdf_hashes[-10:]
                if reader_mode:
                    last_reader_image_hash = hashlib.sha256(reader_png).hexdigest() if reader_png is not None else None
                    marker = get_reader_position(page)
                    recovery_attempts = 0
                    k_total = get_reader_total_pages(page)
                    if k_total is not None:
                        expected_total_pages = k_total
                    last_reader_page = marker[1] if marker else None
                    k_pct = get_reader_progress_percent(page)
                    if k_pct is not None:
                        last_reader_percent = k_pct
                    if expected_total_pages is not None:
                        progress_current = last_reader_page if last_reader_page is not None else page_idx
                        log(f"READER_PROGRESS={progress_current}/{expected_total_pages}")
                    if reader_checkpoint is not None:
                        reader_checkpoint.append(page_idx, ReaderCapture(
                            page_pdf_bytes, last_reader_page, last_reader_image_hash,
                            expected_total_pages, last_reader_percent,
                            page.viewport_size["height"] if created_page and page.viewport_size else None,
                        ))
                        if clean_single_turn and page_idx > 1:
                            reader_checkpoint.save_verified_gap(page_idx - 1, b"")
                elif reader_checkpoint is not None:
                    reader_checkpoint.append(page_idx, ReaderCapture(
                        page_pdf_bytes, page_idx, current_pdf_hash,
                        expected_total_pages, None, None,
                    ))
                    if page.url:
                        reader_checkpoint.save_resume_url(page.url, skipped_blank_steps)
                collected_page_pdfs.append(page_pdf_bytes)
                consecutive_blank_skips = 0
                log(f"[Browser] Captured page {page_idx} via PDF command ({len(page_pdf_bytes)} bytes)")

                if reader_mode:
                    if (
                        (last_reader_percent is not None and last_reader_percent >= 95)
                        or (
                            expected_total_pages is not None
                            and expected_total_pages > 0
                            and last_reader_page is not None
                            and (last_reader_page / expected_total_pages) >= 0.95
                        )
                    ):
                        prev_was_near_end = True
                    # Stop immediately once the very last page is captured so we never press turn_key
                    # into Kindle's next-volume free sample or store upsell screen.
                    if (
                        page_idx > 3
                        and expected_total_pages is not None
                        and last_reader_page is not None
                        and last_reader_page >= expected_total_pages
                        and (last_reader_percent is None or last_reader_percent >= 99)
                    ):
                        log(f"[Reader] Reached final page ({last_reader_page}/{expected_total_pages}); stopping cleanly before next-volume sample.")
                        break

                if len(collected_page_pdfs) == max_pages:
                    break
                url_before_turn = page.url
                if reader_mode:
                    source = page.evaluate(READER_PAGE_SOURCE)
                    page.keyboard.press(turn_key)
                    if source:
                        # Let the next iteration handle unchanged pages and reader errors.
                        turned = wait_for_reader_turn(page, source)
                        clean_single_turn = bool(turned and not is_reader_oops(page))
                        continue
                    clean_single_turn = False
                    if is_reader_oops(page):
                        continue
                else:
                    page.keyboard.press(turn_key)
                page.wait_for_timeout(500)
                wait_for_page_ready(page, timeout_ms=8000)

                # If a PDF response was triggered by the page turn, prefer collected PDFs if complete
                if len(captured_pdfs) > pdf_count_before:
                    log(f"[Browser] Intercepted PDF response after page turn {page_idx}.")

                # Fallback: if ArrowRight key did not change the URL or view, try clicking a right-arrow / next button
                after_key_hash = hashlib.sha256(_take_screenshot_resilient(page, timeout_ms=15000)).hexdigest()
                if page.url == url_before_turn and after_key_hash == current_hash:
                    for selector in arrow_button_selectors:
                        locator = page.locator(selector).first
                        try:
                            if locator.count() > 0 and locator.is_visible():
                                locator.click(timeout=3000)
                                page.wait_for_timeout(500)
                                wait_for_page_ready(page, timeout_ms=8000)
                                break
                        except Exception:
                            continue

            if reader_mode and hasattr(signal, "SIGALRM"):
                signal.alarm(0)
            if not reader_mode and captured_pdfs and len(captured_pdfs) >= len(collected_page_pdfs):
                written = merge_pdf_bytes_list(captured_pdfs, resolved_output)
            elif reader_mode and reader_checkpoint is not None:
                captures = reader_checkpoint.load()
                if captures and reader_at_last_page(captures[-1], saved_count=len(captures)):
                    return finish_reader_at_end(start_url, resolved_output, turn_key, reader_checkpoint, captures, result_details, is_reflowable=reader_is_reflowable)
                collected_page_pdfs = normalize_reader_pages_to_cover_size(collected_page_pdfs, is_reflowable=reader_is_reflowable)
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=resolved_output.parent) as folder:
                    staged_output = Path(folder) / resolved_output.name
                    written = merge_pdf_bytes_list(collected_page_pdfs, staged_output)
                    check_reader_completeness(
                        written, expected_total_pages, last_reader_page, last_reader_percent, max_pages,
                        skipped_blank_steps=skipped_blank_steps,
                    )
                    verify_merged_reader_pdf(collected_page_pdfs, staged_output, written)
                    staged_output.replace(resolved_output)
                    if result_details is not None:
                        result_details["verifiedPages"] = written
            else:
                if reader_mode:
                    collected_page_pdfs = normalize_reader_pages_to_cover_size(collected_page_pdfs, is_reflowable=reader_is_reflowable)
                written = merge_pdf_bytes_list(collected_page_pdfs, resolved_output)
            captured_steps = len(collected_page_pdfs) + skipped_blank_steps
            if not reader_mode and "learning.oreilly.com" in (page.url or ""):
                current_leaf = (page.url or "").split("#")[0].split("?")[0].rstrip("/").split("/")[-1]
                if oreilly_last_filename and current_leaf == oreilly_last_filename and captured_steps > 0:
                    expected_total_pages = captured_steps
                elif expected_total_pages is not None and captured_steps < expected_total_pages:
                    m_final_ch = re.search(r"chapter-(\d+)\.html", current_leaf)
                    if m_final_ch and int(m_final_ch.group(1)) >= expected_total_pages:
                        captured_steps = expected_total_pages
            is_complete_non_reader = (
                expected_total_pages is None or captured_steps >= expected_total_pages
            )
            try:
                qc = verify_exported_pdf(
                    resolved_output,
                    expected_pages=expected_total_pages if last_reader_page != expected_total_pages else None,
                    last_percent=last_reader_percent,
                    captured_steps=captured_steps if not reader_mode else None,
                )
                log(f"[Verify] {qc['summary']}")
                log(f"QUALITY_CHECK={qc['summary']}")
                if result_details is not None:
                    if qc.get("loading_pages"):
                        result_details["loadingPages"] = len(qc["loading_pages"])
            except Exception as qc_exc:
                log(f"[Verify] Skipped ({qc_exc})")
            if reader_checkpoint is not None and (reader_mode or is_complete_non_reader):
                reader_checkpoint.remove()
            if result_details is not None and expected_total_pages is not None:
                result_details["expectedPages"] = expected_total_pages
                if last_reader_page == expected_total_pages:
                    result_details["lastPrintedPage"] = last_reader_page
                    result_details["unverifiedGaps"] = 0
                elif not reader_mode:
                    result_details["lastPrintedPage"] = captured_steps
            return written, resolved_output
        finally:
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)
            if created_page:
                _close_page_safely(page, active_cdp_url, page_target_id)
            elif reused_dedicated_tab and use_cdp:
                try:
                    if not page.is_closed():
                        page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
                except Exception:
                    pass
            if not use_cdp:
                try:
                    browser.close()
                except Exception:
                    pass


def fetch_and_merge(
    url: str,
    output_path: Path,
    max_pages: int = 1000,
    timeout: int = 30,
    allow_browser: bool = True,
    direction: str = "right",
    auto_name: bool = False,
) -> int:
    """Fetch PDF(s) from url (with pagination) and write a single merged PDF to output_path."""
    if not is_http_url(url):
        raise ValueError(f"Invalid URL (must start with http:// or https://): {url}")

    session = create_session()

    # Step 1: Direct PDF check
    log(f"[Fetch] Checking URL: {url}")
    direct_blob = download_if_pdf(session, url, timeout=timeout)
    if direct_blob is not None:
        log("[Fetch] Direct PDF detected.")
        return merge_pdf_bytes_list([direct_blob], output_path)

    # Step 2: HTML pagination + PDF link crawl
    pdf_urls = crawl_html_for_pdfs(session, url, max_pages=max_pages, timeout=timeout)
    if pdf_urls:
        log(f"[Download] Downloading {len(pdf_urls)} PDF file(s)...")
        blobs: list[bytes] = []
        for idx, pdf_url in enumerate(pdf_urls, start=1):
            log(f"  [{idx}/{len(pdf_urls)}] {pdf_url}")
            blob = download_if_pdf(session, pdf_url, timeout=timeout)
            if blob is not None:
                blobs.append(blob)
        if blobs:
            return merge_pdf_bytes_list(blobs, output_path)

    # Step 3: Browser fallback for JS viewers / embedded PDFs
    if allow_browser:
        log("[Fetch] No static PDF links found. Trying headless browser...")
        pages, _ = fetch_via_browser(
            url,
            output_path,
            max_pages=max_pages,
            timeout=timeout,
            direction=direction,
            auto_name=auto_name,
        )
        return pages

    raise RuntimeError(f"No PDF content found at URL: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch PDF(s) from a URL with pagination and merge into one PDF.")
    parser.add_argument("--url", required=True, help="Source URL")
    parser.add_argument("--output", help="Output merged PDF path")
    parser.add_argument("--max-pages", type=int, default=1000, help="Maximum pages to paginate (default: 1000)")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30)")
    parser.add_argument("--direction", choices=["right", "left"], default="right", help="Page-turn key direction (default: right)")
    parser.add_argument("--aspect-preset", choices=["auto", "ipad", "iphone", "pc"], default="auto", help="Preferred viewport aspect ratio for reflowable books (default: auto)")
    parser.add_argument("--quality", choices=["standard", "high"], default="high", help="Capture resolution: standard (1x) or high (2x Retina) (default: high)")
    parser.add_argument("--no-browser", action="store_true", help="Disable Playwright browser fallback")
    parser.add_argument("--auto-name", action="store_true", help="Auto-name output as <Title>_<YYYYMMDD>.pdf when available")
    parser.add_argument("--reader-checkpoint", type=Path, help="Save and resume captured Reader pages")
    parser.add_argument("--result-json", type=Path, help="Write the output path and page count for the app")
    parser.add_argument("--inspect-reader-total", action="store_true", help="Read only the ebook's total page count")
    args = parser.parse_args()

    if args.inspect_reader_total:
        if args.result_json is None:
            parser.error("--inspect-reader-total requires --result-json")
        try:
            total = inspect_reader_total_pages(args.url)
            if total is None:
                raise RuntimeError("Reader did not show a total page count.")
            args.result_json.write_text(json.dumps({"expectedPages": total}), encoding="utf-8")
            return 0
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr, flush=True)
            return 1

    if not args.output:
        parser.error("--output is required for PDF capture")
    result_details: dict[str, int] = {}
    output_path = Path(args.output).expanduser().resolve()
    final_path = output_path
    checkpoint = ReaderCheckpoint(args.reader_checkpoint) if args.reader_checkpoint else None
    try:
        if checkpoint is not None and not args.no_browser:
            if not is_http_url(args.url.strip()):
                raise ValueError("Invalid source URL")
            consecutive_stalls = 0
            while True:
                before_count, _ = checkpoint.progress()
                try:
                    total_pages, final_path = fetch_via_browser(
                        args.url.strip(), output_path,
                        max_pages=max(1, args.max_pages), timeout=max(5, args.timeout),
                        direction=args.direction, auto_name=args.auto_name, reader_checkpoint=checkpoint,
                        result_details=result_details,
                        aspect_preset=args.aspect_preset,
                        quality=args.quality,
                    )
                    break
                except (ReaderStepTimeout, Exception) as exc:
                    if isinstance(exc, (CaptureCanceled, ValueError, IncompleteReaderCapture)):
                        raise
                    exc_text = f"{type(exc).__name__}: {exc}".lower()
                    is_recoverable_timeout = (
                        isinstance(exc, ReaderStepTimeout)
                        or "timeout" in exc_text
                        or "target closed" in exc_text
                        or "execution context" in exc_text
                        or "connection closed" in exc_text
                    )
                    if not is_recoverable_timeout:
                        raise
                    after_count, _ = checkpoint.progress()
                    if after_count > before_count:
                        consecutive_stalls = 0
                    consecutive_stalls += 1
                    if consecutive_stalls > 3:
                        raise
                    log(f"[Reader] Transient timeout/error ({type(exc).__name__}) at {after_count} saved page(s); auto-resuming in a fresh tab ({consecutive_stalls}/3)...")
                    time.sleep(3)
        elif args.auto_name and not args.no_browser:
            # Allow fetch_and_merge / fetch_via_browser to resolve final output path
            if not is_http_url(args.url.strip()):
                raise ValueError(f"Invalid URL (must start with http:// or https://): {args.url.strip()}")
            session = create_session()
            log(f"[Fetch] Checking URL: {args.url.strip()}")
            direct_blob = download_if_pdf(session, args.url.strip(), timeout=max(5, args.timeout))
            if direct_blob is not None:
                log("[Fetch] Direct PDF detected.")
                total_pages = merge_pdf_bytes_list([direct_blob], output_path)
            else:
                pdf_urls = crawl_html_for_pdfs(session, args.url.strip(), max_pages=max(1, args.max_pages), timeout=max(5, args.timeout))
                if pdf_urls:
                    blobs = [b for u in pdf_urls if (b := download_if_pdf(session, u, timeout=max(5, args.timeout))) is not None]
                    if not blobs:
                        raise RuntimeError(f"No PDF content found at URL: {args.url.strip()}")
                    total_pages = merge_pdf_bytes_list(blobs, output_path)
                else:
                    log("[Fetch] No static PDF links found. Trying headless browser...")
                    total_pages, final_path = fetch_via_browser(
                        args.url.strip(),
                        output_path,
                        max_pages=max(1, args.max_pages),
                        timeout=max(5, args.timeout),
                        direction=args.direction,
                        auto_name=True,
                        result_details=result_details,
                        aspect_preset=args.aspect_preset,
                    )
        else:
            total_pages = fetch_and_merge(
                url=args.url.strip(),
                output_path=output_path,
                max_pages=max(1, args.max_pages),
                timeout=max(5, args.timeout),
                allow_browser=not args.no_browser,
                direction=args.direction,
                auto_name=args.auto_name,
            )
    except CaptureCanceled:
        if args.result_json and checkpoint is not None:
            count, total = checkpoint.progress()
            if count:
                args.result_json.write_text(json.dumps({
                    "pageCount": count,
                    "expectedPages": total,
                    "message": "取得を中断しました。",
                }), encoding="utf-8")
        log("[Fetch] Canceled.")
        return 130
    except ReaderStepTimeout:
        if args.result_json and checkpoint is not None:
            count, total = checkpoint.progress()
            if count:
                args.result_json.write_text(json.dumps({
                    "pageCount": count,
                    "expectedPages": total,
                    "message": f"ページの応答が 60 秒以上止まったため中断しました（取得済み: {count} ページ）。",
                }), encoding="utf-8")
        print("Error: Reader page step timed out after 60s.", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        if args.result_json and isinstance(exc, IncompleteReaderCapture):
            args.result_json.write_text(json.dumps({
                "pageCount": exc.page_count,
                "expectedPages": exc.expected_pages,
                "message": str(exc),
            }), encoding="utf-8")
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1

    if args.result_json:
        args.result_json.write_text(json.dumps({
            "outputPath": str(final_path),
            "pageCount": total_pages,
            **result_details,
        }), encoding="utf-8")
    log(f"OUTPUT_PDF={final_path}")
    log(f"PAGES={total_pages}")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancel_capture)
    sys.exit(main())
