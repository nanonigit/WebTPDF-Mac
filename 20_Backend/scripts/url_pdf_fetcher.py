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


def is_kindle_cloud_reader(page) -> bool:
    """Return True if the page is Amazon Kindle Cloud Reader."""
    try:
        if "read.amazon." in (page.url or "").lower():
            return True
        return page.locator("img[src^='blob:https://read.amazon.']").count() > 0
    except Exception:
        return False


def dismiss_kindle_dialogs(page) -> bool:
    """Dismiss Kindle Cloud Reader modal prompts (e.g. 'Most Recent Page Read') if visible.

    Returns True if a 'Most Recent Page Read' prompt was dismissed.
    """
    try:
        body_text = (page.inner_text("body") or "").lower()
        is_recent_prompt = (
            "most recent page read" in body_text
            or "most recent page" in body_text
            or "最後に読んだページ" in body_text
        )
        no_btn = page.locator("button:has-text('No'), button:has-text('いいえ')").first
        if no_btn.count() > 0 and no_btn.is_visible():
            no_btn.click(timeout=1500)
            page.wait_for_timeout(400)
            return is_recent_prompt
    except Exception:
        pass
    return False


def is_kindle_not_at_start(page) -> bool:
    """Return True if Kindle Cloud Reader is currently past page 1 / > 0%."""
    try:
        body_text = page.inner_text("body") or ""
        cur = get_kindle_page_number(page)
        if cur is not None and cur > 1:
            return True
        m_pct = re.search(r"●\s*([1-9]\d*)%", body_text)
        if m_pct:
            return True
    except Exception:
        pass
    return False


def rewind_kindle_to_start(page, turn_key: str, max_steps: int = 600) -> None:
    """Step backwards in Kindle Cloud Reader until reaching the first page (cover)."""
    reverse_key = "ArrowRight" if turn_key == "ArrowLeft" else "ArrowLeft"
    log(f"[Kindle] Not at first page; rewinding to start via {reverse_key}...")
    unchanged = 0
    for step in range(max_steps):
        dismiss_kindle_dialogs(page)
        prev_src = page.evaluate("() => document.querySelector('img[src^=\"blob:\"]')?.src || ''")
        page.keyboard.press(reverse_key)
        page.wait_for_timeout(160)
        changed = False
        try:
            page.wait_for_function(
                "(prev) => { const img = document.querySelector('img[src^=\"blob:\"]'); return img && img.complete && img.src !== prev; }",
                arg=prev_src,
                timeout=800,
            )
            changed = True
        except Exception:
            cur_src = page.evaluate("() => document.querySelector('img[src^=\"blob:\"]')?.src || ''")
            changed = bool(cur_src and cur_src != prev_src)
        if changed:
            unchanged = 0
        else:
            unchanged += 1
            page.wait_for_timeout(300)
            if unchanged >= 3:
                log(f"[Kindle] Reached first page after {step + 1} backward step(s).")
                return
    log(f"[Kindle] Rewind stopped after {max_steps} steps.")


def is_kindle_oops(page) -> bool:
    """Return True if Kindle Cloud Reader is showing 'Oops... Something Went Wrong'."""
    try:
        back_btn = page.locator("button:has-text('Back to Library'), a:has-text('Back to Library')").first
        if back_btn.count() > 0 and back_btn.is_visible():
            return True
        body_text = (page.inner_text("body") or "").lower()
        return "something went wrong" in body_text and "back to library" in body_text
    except Exception:
        return False


def get_kindle_position(page) -> tuple[str, int] | None:
    """Read the footer's printed Page or Kindle Location and keep the units distinct."""
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


def get_kindle_page_number(page) -> int | None:
    position = get_kindle_position(page)
    return position[1] if position else None


def get_kindle_total_pages(page) -> int | None:
    """Extract total page count from Kindle Cloud Reader footer when available."""
    try:
        body_text = page.inner_text("body", timeout=1000) or ""
        m = re.search(r"Page\s+\d+\s+of\s+(\d+)", body_text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m_ja = re.search(r"(\d+)\s*ページ中\s*\d+\s*ページ", body_text)
        if m_ja:
            return int(m_ja.group(1))
    except Exception:
        pass
    return None


KINDLE_READER_HOSTS = {
    "read.amazon.co.jp", "read.amazon.com", "read.amazon.co.uk", "read.amazon.ca",
    "read.amazon.de", "read.amazon.fr", "read.amazon.it", "read.amazon.es",
    "read.amazon.com.au", "read.amazon.in", "read.amazon.com.br", "read.amazon.com.mx",
}


def inspect_kindle_total_pages(url: str) -> int | None:
    """Read the book's page count from a temporary tab in the logged-in browser."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in KINDLE_READER_HOSTS or parsed.username or parsed.password:
        raise ValueError("Expected a Kindle Cloud Reader URL")
    endpoint = resolve_cdp_url()
    if endpoint is None:
        raise RuntimeError("Open the app's login browser and sign in to Kindle first.")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(endpoint)
        page = browser.contexts[0].new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if is_kindle_oops(page):
                    return None
                total = get_kindle_total_pages(page)
                if total is not None:
                    return total
                page.wait_for_timeout(400)
            return None
        finally:
            page.close()


def get_kindle_progress_percent(page) -> int | None:
    """Extract reading progress percentage (0-100) from Kindle Cloud Reader footer."""
    try:
        body_text = page.inner_text("body") or ""
        m = re.search(r"●\s*(\d{1,3})%", body_text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def verify_exported_pdf(
    pdf_path: Path,
    expected_pages: int | None = None,
    last_percent: int | None = None,
) -> dict:
    """Verify exported PDF for page count, blank pages, edge clipping, and readability margins."""
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        page_count = len(doc)
        blank_pages: list[int] = []
        clipped_pages: list[int] = []
        min_margin_ratio = 1.0

        for idx, page in enumerate(doc):
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
        if expected_pages is not None and page_count < expected_pages:
            warnings.append(f"ページ不足({page_count}/{expected_pages}p)")
        elif last_percent is not None and last_percent < 95:
            warnings.append(f"途中終了の疑い(進行率{last_percent}%)")
        if blank_pages:
            warnings.append(f"空白ページ({len(blank_pages)}p)")
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
            "clipped_pages": clipped_pages,
            "min_margin_ratio": min_margin_ratio,
            "summary": summary,
        }
    finally:
        doc.close()


class CaptureCanceled(BaseException):
    """Let cleanup run on a Stop request without treating it as a retryable reader error."""


def cancel_capture(_signum, _frame) -> None:
    raise CaptureCanceled()


class IncompleteKindleCapture(RuntimeError):
    def __init__(self, message: str, page_count: int, expected_pages: int | None):
        super().__init__(message)
        self.page_count = page_count
        self.expected_pages = expected_pages


def check_kindle_completeness(
    page_count: int,
    expected_pages: int | None,
    last_page: int | None,
    last_percent: int | None,
    max_pages: int,
) -> None:
    if expected_pages is not None:
        incomplete = (last_page < expected_pages) if last_page is not None else (page_count < expected_pages)
    else:
        incomplete = last_percent is not None and last_percent < 95
    if incomplete:
        message = "最大ページ数に達しました。上限を増やしてください。" if page_count >= max_pages else "本の最後まで取得できませんでした。"
        raise IncompleteKindleCapture(message, page_count, expected_pages)


@dataclass(frozen=True)
class KindleCapture:
    pdf: bytes
    position: int | None
    image_hash: str | None
    total: int | None
    percent: int | None
    viewport_height: int | None


class KindleCheckpoint:
    """Save each captured page and its reader position in the same transaction."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[KindleCapture]:
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pages'").fetchone() is None:
                return []
            rows = db.execute(
                "SELECT pdf, position, image_hash, total, percent, viewport_height "
                "FROM pages ORDER BY ordinal"
            )
            return [KindleCapture(*row) for row in rows]

    def append(self, ordinal: int, capture: KindleCapture) -> None:
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
            db.execute("INSERT INTO verified_gaps VALUES (?, ?)", (ordinal, pdf))

    def final_path(self, proposed: Path) -> Path:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS export_path (path TEXT NOT NULL)")
            existing = db.execute("SELECT path FROM export_path").fetchone()
            if existing:
                return Path(existing[0])
            db.execute("INSERT INTO export_path VALUES (?)", (str(proposed),))
            return proposed

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


def _wait_for_kindle_reader(page, timeout_ms: int = 15000, previous_source: str = "") -> bool:
    """Wait until Kindle reader renders a blob page image or shows an Oops dialog."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        dismiss_kindle_dialogs(page)
        if is_kindle_oops(page):
            return False
        source = page.evaluate(KINDLE_PAGE_SOURCE)
        if source and source != previous_source:
            return True
        page.wait_for_timeout(250)
    return False


KINDLE_PAGE_SOURCE = """() => {
    const images = [...document.querySelectorAll('img[src^="blob:"]')];
    const pages = images.map(img => ({img, box: img.getBoundingClientRect()}))
        .filter(({img, box}) => img.complete && img.naturalWidth
            && box.width > 100 && box.height > 100 && box.right > 0 && box.bottom > 0
            && box.left < innerWidth && box.top < innerHeight);
    pages.sort((a, b) => b.box.width * b.box.height - a.box.width * a.box.height);
    return pages[0]?.img.src || '';
}"""


def wait_for_kindle_turn(page, previous_source: str, timeout_ms: int = 5000) -> bool:
    """Wait for a decoded page image, or return as soon as the error dialog appears."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if is_kindle_oops(page):
            return False
        source = page.evaluate(KINDLE_PAGE_SOURCE)
        if source and source != previous_source:
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(250)


KINDLE_TOC_NAME = re.compile(r"^(?:Table of Contents|Contents|目次)$", re.IGNORECASE)


def jump_to_kindle_chapter(page, offset: int) -> bool:
    """Open the reader's TOC and select the current or immediately following chapter."""
    previous_source = page.evaluate(KINDLE_PAGE_SOURCE)
    toggle = page.get_by_role("button", name=KINDLE_TOC_NAME).or_(
        page.locator("#kr-toc-button, #kindle-toc-button, [data-testid='toc-button']")
    )
    for button in toggle.all():
        if button.is_visible():
            button.click(timeout=2000)
            break
    else:
        return False

    heading = page.get_by_role("heading", name=KINDLE_TOC_NAME).filter(visible=True).first
    if not heading.count():
        heading = page.get_by_text(KINDLE_TOC_NAME, exact=True).filter(visible=True).last
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
                if offset == 0 and page.evaluate(KINDLE_PAGE_SOURCE) and not is_kindle_oops(page):
                    return True
                return wait_for_kindle_turn(page, previous_source)
            panel = panel.locator("xpath=..")
        return False
    finally:
        if heading.is_visible() and button.is_visible():
            button.click(timeout=2000)


KINDLE_GO_TO_PAGE = re.compile(r"^(?:Go to Page|ページ[へに]移動)$", re.IGNORECASE)
KINDLE_GO_TO_LOCATION = re.compile(r"^(?:Go to Location|位置(?:番号)?[へに]移動)$", re.IGNORECASE)


def kindle_number_fields(panel):
    field = panel.locator("input[type=number], input[placeholder='location' i], input[placeholder='page' i]").filter(visible=True)
    go = panel.get_by_text(re.compile(r"^(?:Go|移動)$", re.I), exact=True).filter(visible=True)
    if field.count() == 1 and go.count() == 1:
        return field, go
    return None


def open_kindle_number_dialog(page, prefer_page: bool):
    # The Ionic reader has an explicit modal. Its Go control is an ion-button host.
    live_panel = page.locator("ion-modal.go-to-modal").filter(visible=True).first
    if live_panel.is_visible():
        controls = kindle_number_fields(live_panel)
        if controls:
            kind = "page" if live_panel.get_by_text(KINDLE_GO_TO_PAGE, exact=True).first.is_visible() else "location"
            return kind, live_panel, *controls

    location = page.get_by_text(KINDLE_GO_TO_LOCATION, exact=True).filter(visible=True).first
    printed_page = page.get_by_text(KINDLE_GO_TO_PAGE, exact=True).filter(visible=True).first
    available = printed_page.or_(location) if prefer_page else location
    if not available.first.is_visible():
        menu_name = re.compile(
            r"^(?:More(?: options| menu| actions| settings)?|Reader menu|Menu|Options|その他(?:のオプション)?|詳細(?:設定)?|メニュー|⋮)$",
            re.I,
        )
        reader_menu = page.locator("ion-button[aria-label='Reader menu'], #reader-header [aria-label='メニュー']").filter(visible=True)
        if not reader_menu.count() and urlparse(page.url).hostname in KINDLE_READER_HOSTS:
            # The top bar is actually removed until the reader canvas is tapped.
            box = _find_kindle_page_box(page)
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
        kind, entry, label = "page", printed_page, KINDLE_GO_TO_PAGE
    elif location.is_visible():
        kind, entry, label = "location", location, KINDLE_GO_TO_LOCATION
    else:
        return None
    entry.click(timeout=2000)
    if live_panel.is_visible():
        controls = kindle_number_fields(live_panel)
        if controls:
            return kind, live_panel, *controls
    heading = page.get_by_text(label, exact=True).filter(visible=True).last
    heading.wait_for(state="visible", timeout=2000)
    panel = heading.locator("xpath=..")
    for _ in range(4):
        if panel.evaluate("node => node === document.body || node === document.documentElement"):
            break
        controls = kindle_number_fields(panel)
        if controls:
            return kind, panel, *controls
        panel = panel.locator("xpath=..")
    page.keyboard.press("Escape")
    return None


def seek_kindle_position(
    page, target: int, target_is_printed_page: bool, expected_total_pages: int | None = None,
) -> bool:
    """Navigate by number first; the caller still verifies the saved screenshot."""
    low, high = 1, None
    next_probe = None
    locations_per_page = None
    for attempt in range(13 if target_is_printed_page else 1):
        before = get_kindle_position(page)
        dialog = open_kindle_number_dialog(page, prefer_page=target_is_printed_page)
        if dialog is None:
            log("[Kindle] No usable Go to Page or Go to Location dialog was found.")
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
                            log("[Kindle] The maximum location number is unavailable.")
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
                log("[Kindle] The reader has no printed page label to compare with the saved page.")
                return False

            previous_source = page.evaluate(KINDLE_PAGE_SOURCE)
            log(f"[Kindle] Entering {kind} number {number} to recover the saved position.")
            field.fill(str(number), timeout=1500)
            go.click(timeout=2000)
            field.wait_for(state="hidden", timeout=2000)
        finally:
            if field.is_visible():
                cancel = panel.get_by_text(re.compile(r"^(?:Cancel|キャンセル)$", re.I), exact=True).filter(visible=True).first
                if cancel.is_visible():
                    cancel.click(timeout=1500)

        changed = wait_for_kindle_turn(page, previous_source, timeout_ms=1000 if same_position else 3500)
        if not changed and (not same_position or is_kindle_oops(page)):
            return False
        actual = get_kindle_position(page)
        if kind == "page":
            deadline = time.monotonic() + 4
            while not actual or actual[0] != "page" or abs(actual[1] - number) > 1:
                if time.monotonic() >= deadline or is_kindle_oops(page):
                    log(f"[Kindle] The page field did not settle near {number}.")
                    return False
                page.wait_for_timeout(250)
                actual = get_kindle_position(page)
        if actual:
            log(f"[Kindle] Number navigation reached {actual[0]} {actual[1]}.")
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


def reopen_kindle_from_library(page, start_url: str, book_title: str | None) -> bool:
    """Use the reader's library button, selecting the exact book by ASIN or title."""
    library_name = re.compile(r"^(Back to Library|Kindle Library|ライブラリに戻る|Kindleライブラリ)$", re.I)
    back = page.get_by_role("button", name=library_name).or_(
        page.get_by_role("link", name=library_name)
    ).filter(visible=True).first
    if not back.is_visible():
        return False

    previous_source = page.evaluate(KINDLE_PAGE_SOURCE)
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
        log("[Kindle] Could not identify a unique library entry for this book.")
        return False
    card.click(timeout=3000)
    if not _wait_for_kindle_reader(page, previous_source=previous_source):
        return False
    opened_url = urlparse(page.url)
    if opened_url.hostname != urlparse(start_url).hostname:
        return False
    opened_asin = parse_qs(opened_url.query).get("asin", [""])[0]
    if opened_asin and opened_asin.upper() != asin.upper():
        log("[Kindle] The library opened a different book.")
        return False
    log("[Kindle] Reopened the book from the library.")
    return True


def matches_saved_kindle_image(screenshot: bytes, saved_pdf: bytes, *, same_printed_page: bool = False) -> bool:
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
                if saved.size != (w + 2 * pad_x, h + 2 * pad_y):
                    return False
                saved = saved.crop((pad_x, pad_y, pad_x + w, pad_y + h))
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


def recover_kindle_oops(
    page,
    start_url: str,
    last_kindle_page: int | None,
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
            log("[Kindle] Trying to reopen the book from the library...")
            try:
                opened_from_library = reopen_kindle_from_library(page, start_url, book_title)
            except Exception as exc:
                log(f"[Kindle] Library reopen did not finish ({type(exc).__name__}).")
        if not opened_from_library:
            response = page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            check_access_denied(page, response.status if response else None)
            if not _wait_for_kindle_reader(page):
                log("[Kindle] The reopened reader did not render a page.")
                return False

        if captured_count == 0:
            if is_kindle_not_at_start(page):
                rewind_kindle_to_start(page, turn_key)
            return _wait_for_kindle_reader(page)

        if use_location and last_kindle_page is not None and last_image_hash is not None:
            try:
                log("[Kindle] Trying number navigation before page turns or table of contents...")
                seek_kindle_position(
                    page, last_kindle_page, expected_total_pages is not None, expected_total_pages,
                )
            except Exception as exc:
                log(f"[Kindle] Go to Location unavailable ({type(exc).__name__}).")
                page.keyboard.press("Escape")
            if is_kindle_oops(page):
                return False

        if toc_offset is not None and last_image_hash is not None and last_kindle_page is not None:
            log("[Kindle] Trying the table of contents to recover the page turn...")
            try:
                jumped = jump_to_kindle_chapter(page, toc_offset)
            except Exception as exc:
                log(f"[Kindle] Table of contents unavailable ({type(exc).__name__}).")
                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                if not _wait_for_kindle_reader(page):
                    return False
                jumped = False
            if jumped and get_kindle_page_number(page) is None:
                return False

        # When the footer is unavailable, start the search from the beginning.
        # A reopened book can restore a later position than the last capture.
        if last_kindle_page is None or get_kindle_page_number(page) is None:
            rewind_kindle_to_start(page, turn_key)
        reverse_key = "ArrowRight" if turn_key == "ArrowLeft" else "ArrowLeft"
        expected_position_kind = "page" if expected_total_pages is not None else None
        previous_direction = None
        reversals = 0
        for _ in range(max(captured_count + 20, 40)):
            if is_kindle_oops(page):
                return False
            marker = get_kindle_position(page)
            position = marker[1] if marker and expected_position_kind in (None, marker[0]) else None
            source = page.evaluate(KINDLE_PAGE_SOURCE)
            if not source:
                return False
            if last_image_hash is not None:
                box = _find_kindle_page_box(page)
                matches_image = False
                if box is not None:
                    frame = page.screenshot(clip=box, timeout=5000)
                    matches_image = hashlib.sha256(frame).hexdigest() == last_image_hash
                    if (
                        not matches_image and last_pdf is not None
                        and marker is not None and marker[0] == expected_position_kind
                        and position == last_kindle_page
                    ):
                        matches_image = matches_saved_kindle_image(frame, last_pdf)
                        if matches_image:
                            log("[Kindle] Verified the saved page by matching its image.")
            else:
                # Reflowable layouts can lack a clipped page image.
                matches_image = last_kindle_page is not None and position == last_kindle_page
            matches_position = last_kindle_page is None or position is None or position == last_kindle_page
            if matches_image and matches_position:
                page.keyboard.press(turn_key)
                if wait_for_kindle_turn(page, source):
                    log(f"[Kindle] Resumed after captured page {captured_count}.")
                    return True
                return False
            backwards = last_kindle_page is not None and position is not None and position > last_kindle_page
            direction = reverse_key if backwards else turn_key
            if previous_direction is not None and direction != previous_direction:
                reversals += 1
                if reversals > 1:
                    log("[Kindle] Stopped seeking after crossing the saved page without verifying its image.")
                    return False
            previous_direction = direction
            page.keyboard.press(direction)
            if not wait_for_kindle_turn(page, source):
                return False
        log("[Kindle] Could not verify the last captured page after reopening.")
        return False
    except Exception as exc:
        log(f"[Kindle] Reader recovery failed ({type(exc).__name__}).")
        return False


def _find_kindle_page_box(page) -> dict | None:
    """Return the bounding box of the largest visible Kindle blob page image."""
    try:
        imgs = page.locator("img[src^='blob:']")
        count = imgs.count()
        best_box = None
        best_area = 0.0
        for i in range(count):
            image = imgs.nth(i)
            if not image.evaluate("(img) => img.complete && img.naturalWidth > 0"):
                continue
            box = image.bounding_box(timeout=1000)
            if not box:
                continue
            w, h = box.get("width", 0.0), box.get("height", 0.0)
            if w > 100 and h > 100 and (w * h) > best_area:
                best_area = w * h
                best_box = box
        return best_box
    except Exception:
        return None



def capture_kindle_page_as_pdf_bytes(page, pad_ratio: float = 0.07) -> tuple[bytes, bytes] | None:
    """Capture the rendered Kindle page blob image via clipped screenshot with comfortable book margins."""
    box = _find_kindle_page_box(page)
    if not box:
        return None

    png_bytes = page.screenshot(clip=box, timeout=15000)
    buf = io.BytesIO()
    with Image.open(io.BytesIO(png_bytes)) as img:
        rgb = img.convert("RGB")
        if pad_ratio > 0:
            pad_x = max(24, int(round(rgb.width * pad_ratio)))
            pad_y = max(32, int(round(rgb.height * pad_ratio)))
            canvas = Image.new("RGB", (rgb.width + 2 * pad_x, rgb.height + 2 * pad_y), (255, 255, 255))
            canvas.paste(rgb, (pad_x, pad_y))
            canvas.save(buf, "PDF")
        else:
            rgb.save(buf, "PDF")
    return buf.getvalue(), png_bytes


def measure_cover_aspect_ratio(png_bytes: bytes) -> float | None:
    """Estimate portrait height/width ratio of non-white cover artwork in png_bytes."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as im:
            bbox = im.convert("RGB").point(lambda x: 0 if x > 245 else 255).getbbox()
            if not bbox:
                return None

            cw = bbox[2] - bbox[0]
            ch = bbox[3] - bbox[1]
            if cw > 100 and ch > 100 and ch > cw:
                return max(1.25, min(1.65, ch / cw))
    except Exception:
        pass
    return None



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
    """Best-effort extraction of (title, YYYYMMDD) from O'Reilly, Kindle, or generic HTML pages."""
    url = page.url or ""
    # 1. O'Reilly Learning metadata API
    m_oreilly = re.search(r"learning\.oreilly\.com/library/view/[^/]+/(\d+)", url)
    if m_oreilly:
        book_id = m_oreilly.group(1)
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

    # 2. Kindle Cloud Reader title & generic meta tags
    try:
        info = page.evaluate(
            """() => {
                const isKindle = location.hostname.includes('read.amazon.');
                let title = '';
                if (isKindle) {
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


def kindle_at_last_page(capture: KindleCapture) -> bool:
    return capture.total is not None and capture.position == capture.total and capture.percent == 100


def kindle_gap_candidates(captures: list[KindleCapture]) -> list[tuple[int, KindleCapture, KindleCapture]]:
    return [
        (ordinal, left, right)
        for ordinal, (left, right) in enumerate(zip(captures, captures[1:]), 1)
        if left.total is not None and left.total == right.total
        and left.position is not None and right.position is not None
        and right.position > left.position + 1
    ]


def kindle_frame_matches(page, saved: KindleCapture) -> bool:
    if not saved.image_hash or get_kindle_position(page) != ("page", saved.position) or is_kindle_oops(page):
        return False
    box = _find_kindle_page_box(page)
    if box is None:
        return False
    frame = page.screenshot(clip=box, timeout=5000)
    return (
        hashlib.sha256(frame).hexdigest() == saved.image_hash
        or matches_saved_kindle_image(frame, saved.pdf, same_printed_page=True)
    )


def follow_kindle_gap(page, start: KindleCapture, end: KindleCapture, key: str, sign: int) -> list[bytes] | None:
    """Follow one direction between two independently matched images."""
    if start.position is None or end.position is None:
        return None

    def advance() -> bool:
        source = page.evaluate(KINDLE_PAGE_SOURCE)
        page.keyboard.press(key)
        if not wait_for_kindle_turn(page, source):
            return False
        page.wait_for_timeout(280)
        return not is_kindle_oops(page)

    current = get_kindle_position(page)
    if not kindle_frame_matches(page, start):
        if not current or current[0] != "page" or not 1 <= (start.position - current[1]) * sign <= 5:
            target = max(1, min(start.total or start.position, start.position - sign))
            if not seek_kindle_position(page, target, True, start.total):
                return None
        for _ in range(8):
            if kindle_frame_matches(page, start):
                break
            marker = get_kindle_position(page)
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
        if kindle_frame_matches(page, end):
            return frames if sign > 0 else frames[::-1]
        marker = get_kindle_position(page)
        if not marker or marker[0] != "page" or not (
            (marker[1] - previous) * sign >= 0 and (end.position - marker[1]) * sign >= 0
        ):
            return None
        screenshot = capture_kindle_page_as_pdf_bytes(page)
        if screenshot is None or is_kindle_oops(page):
            return None
        pdf, png = screenshot
        digest = hashlib.sha256(png).hexdigest()
        if digest in seen:
            return None
        seen.add(digest)
        frames.append(pdf)
        previous = marker[1]
    return None


def verify_kindle_gap(page, left: KindleCapture, right: KindleCapture, turn_key: str) -> bytes | None:
    """Verify both saved images and return the PDF pages seen between them."""
    if not left.image_hash or not right.image_hash:
        return None
    frames = follow_kindle_gap(page, left, right, turn_key, 1)
    if frames is None and not is_kindle_oops(page):
        reverse_key = "ArrowRight" if turn_key == "ArrowLeft" else "ArrowLeft"
        frames = follow_kindle_gap(page, right, left, reverse_key, -1)
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


def verify_merged_kindle_pdf(pdfs: list[bytes], output: Path, count: int) -> None:
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


def finish_kindle_at_end(
    start_url: str, proposed_output: Path, turn_key: str,
    checkpoint: KindleCheckpoint, captures: list[KindleCapture],
    result_details: dict[str, int] | None,
) -> tuple[int, Path]:
    """Check potential gaps within the saved sequence and publish a single PDF."""
    gaps = kindle_gap_candidates(captures)
    verified = checkpoint.verified_gaps()
    todo = [(i, left, right) for i, left, right in gaps if i not in verified]
    unchecked = len(todo)
    output = checkpoint.final_path(proposed_output)
    log(f"KINDLE_UNVERIFIED_GAPS={unchecked}")
    if todo:
        log(f"[Kindle] Last page reached. Checking {len(todo)} intervals for missing views.")
        try:
            from playwright.sync_api import sync_playwright

            endpoint = resolve_cdp_url()
            if endpoint is None:
                raise RuntimeError("The Kindle browser session is not available")
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(endpoint)
                page = browser.contexts[0].new_page()
                try:
                    page.set_viewport_size({"width": 800, "height": captures[-1].viewport_height or 1140})
                    page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                    if not _wait_for_kindle_reader(page):
                        raise RuntimeError("The book did not reopen")
                    for index, (ordinal, left, right) in enumerate(todo, 1):
                        log(f"[Kindle] Checking interval {index}/{len(todo)} (Kindle {left.position}–{right.position}).")
                        try:
                            repaired = verify_kindle_gap(page, left, right, turn_key)
                        except Exception as exc:
                            log(f"[Kindle] Interval {left.position}–{right.position}: {type(exc).__name__}")
                            repaired = None
                        if repaired is None:
                            log(f"[Kindle] Could not verify the saved views for {left.position}–{right.position}.")
                            if is_kindle_oops(page):
                                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
                                if not _wait_for_kindle_reader(page):
                                    break
                        else:
                            checkpoint.save_verified_gap(ordinal, repaired)
                            verified[ordinal] = repaired
                            unchecked -= 1
                            log(f"KINDLE_UNVERIFIED_GAPS={unchecked}")
                finally:
                    page.close()
        except Exception as exc:
            log(f"[Kindle] Some intervals could not be checked: {exc}")

    pdfs = []
    for ordinal, capture in enumerate(captures, 1):
        pdfs.append(capture.pdf)
        if verified.get(ordinal):
            pdfs.append(verified[ordinal])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        staged = Path(folder) / output.name
        written = merge_pdf_bytes_list(pdfs, staged)
        verify_merged_kindle_pdf(pdfs, staged, written)
        staged.replace(output)
    log(f"[Kindle] Finalized {written} PDF pages; {unchecked} intervals could not be verified.")
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
    kindle_checkpoint: KindleCheckpoint | None = None,
    result_details: dict[str, int] | None = None,
) -> tuple[int, Path]:
    """Use CDP (logged-in Chrome) or headless Chromium to paginate with ArrowRight/ArrowLeft and capture PDFs."""
    turn_key = "ArrowLeft" if direction == "left" else "ArrowRight"
    saved_captures = kindle_checkpoint.load() if kindle_checkpoint and kindle_checkpoint.path.exists() else []
    if saved_captures and kindle_at_last_page(saved_captures[-1]):
        return finish_kindle_at_end(start_url, output_path, turn_key, kindle_checkpoint, saved_captures, result_details)

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
            is_kindle_url = "read.amazon." in start_url.lower()
            page = None if is_kindle_url else next((pg for pg in context.pages if pg.url == start_url), None)
            created_page = False
            if page is None:
                page = context.new_page()
                created_page = True
        else:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 1600},
            )
            page = context.new_page()
            created_page = True

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
            if created_page and "read.amazon." in start_url.lower():
                try:
                    page.set_viewport_size({"width": 800, "height": 1140})
                except Exception:
                    pass
            response_status: int | None = None
            if created_page or page.url != start_url:
                log(f"[Browser] Opening {start_url} ...")
                nav_resp = page.goto(start_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                response_status = nav_resp.status if nav_resp else None
            else:
                log(f"[Browser] Using already-open tab: {page.url}")
                try:
                    page.bring_to_front()
                except Exception:
                    pass

            log("[Browser] Waiting for page render...")
            wait_for_page_ready(page, timeout_ms=12000)
            check_access_denied(page, response_status)
            resolved_output = output_path
            kindle_mode = is_kindle_cloud_reader(page)
            detected_title, detected_pub = None, None
            reader_error_at_open = kindle_mode and is_kindle_oops(page)
            if (auto_name or kindle_mode) and not reader_error_at_open:
                detected_title, detected_pub = extract_book_title_and_date(page)
            if auto_name:
                resolved_output = resolve_auto_output_path(output_path, detected_title, detected_pub)
                if resolved_output != output_path:
                    log(f"[AutoName] Output filename -> {resolved_output.name}")
            book_title = detected_title if kindle_mode else None
            saved_pages = saved_captures if kindle_mode else []
            if saved_pages:
                log(f"[Kindle] Loaded {len(saved_pages)} saved page(s); resuming capture.")
                if saved_pages[-1].viewport_height is not None:
                    page.set_viewport_size({"width": 800, "height": saved_pages[-1].viewport_height})
            elif kindle_mode:
                dismiss_kindle_dialogs(page)
                if created_page:
                    try:
                        page.set_viewport_size({"width": 800, "height": 1140})
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
            prev_hash: str | None = None
            prev_pdf_hash: str | None = None
            last_kindle_page: int | None = None
            expected_total_pages: int | None = None
            last_kindle_percent: int | None = None
            last_kindle_image_hash: str | None = None
            recovery_attempts = 0
            needs_recovery = bool(saved_pages)
            if saved_pages:
                last_capture = saved_pages[-1]
                prev_pdf_hash = pdf_content_signature(last_capture.pdf)
                last_kindle_page = last_capture.position
                last_kindle_image_hash = last_capture.image_hash
                expected_total_pages = last_capture.total
                last_kindle_percent = last_capture.percent
            if len(saved_pages) > max_pages:
                raise ValueError(f"最大ページ数を取得済みの {len(saved_pages)} ページ以上に設定してください。")

            while len(collected_page_pdfs) < max_pages:
                page_idx = len(collected_page_pdfs) + 1
                kindle_png = None
                if kindle_mode:
                    dismiss_kindle_dialogs(page)
                    if needs_recovery or is_kindle_oops(page):
                        if last_kindle_page is not None and last_kindle_page == expected_total_pages and last_kindle_percent == 100:
                            break
                        if recovery_attempts >= 4:
                            raise IncompleteKindleCapture(
                                f"Kindleのエラーから復旧できませんでした（取得済み: {page_idx - 1} ページ）。",
                                page_idx - 1, expected_total_pages,
                            )
                        recovery_attempts += 1
                        log(f"[Kindle] Recovering before capture {page_idx} ({recovery_attempts}/4)...")
                        recovery_options = dict(
                            captured_count=page_idx - 1,
                            last_image_hash=last_kindle_image_hash,
                            toc_offset=1 if recovery_attempts == 4 else None,
                            book_title=book_title,
                            use_location=recovery_attempts < 4,
                            expected_total_pages=expected_total_pages,
                            last_pdf=collected_page_pdfs[-1] if collected_page_pdfs else None,
                        )
                        if recovery_attempts == 1:
                            recovered = recover_kindle_oops(
                                page, start_url, last_kindle_page, turn_key, **recovery_options,
                            )
                        else:
                            log("[Kindle] Trying a new tab with the current browser session...")
                            recovered = False
                            candidate = None
                            try:
                                candidate = context.new_page()
                                if page.viewport_size:
                                    candidate.set_viewport_size(page.viewport_size)
                                elif created_page:
                                    candidate.set_viewport_size({"width": 800, "height": 1140})
                                recovered = recover_kindle_oops(
                                    candidate, start_url, last_kindle_page, turn_key,
                                    fresh_tab=True, **recovery_options,
                                )
                            except Exception as exc:
                                log(f"[Kindle] New-tab recovery failed ({type(exc).__name__}).")
                            finally:
                                if candidate is not None and not recovered:
                                    try:
                                        candidate.close()
                                    except Exception:
                                        pass
                            if recovered:
                                previous_page, previous_owned = page, created_page
                                page, created_page = candidate, True
                                page.on("response", handle_response)
                                cdp_session = None
                                try:
                                    cdp_session = context.new_cdp_session(page)
                                except Exception:
                                    pass
                                if previous_owned:
                                    try:
                                        previous_page.close()
                                    except Exception:
                                        pass
                                log("[Kindle] Switched capture to the verified new tab.")
                        needs_recovery = not recovered
                        if recovered and book_title is None:
                            book_title, published = extract_book_title_and_date(page)
                            if auto_name and resolved_output == output_path and book_title:
                                resolved_output = resolve_auto_output_path(output_path, book_title, published)
                        if needs_recovery and not page.is_closed():
                            page.wait_for_timeout(1000)
                        continue
                    if page_idx == 1 and recovery_attempts == 0:
                        _wait_for_kindle_reader(page, timeout_ms=15000)
                        if is_kindle_oops(page):
                            continue
                        if is_kindle_not_at_start(page):
                            rewind_kindle_to_start(page, turn_key)
                        if is_kindle_oops(page):
                            continue
                else:
                    wait_for_page_ready(page, timeout_ms=12000)
                png_bytes = page.screenshot(full_page=False, timeout=15000)
                current_hash = hashlib.sha256(png_bytes).hexdigest()

                if prev_hash == current_hash:
                    if kindle_mode:
                        # Retry page turn on slow networks / chapter boundaries before stopping
                        max_turn_retries = 3 if (expected_total_pages and last_kindle_page and last_kindle_page < expected_total_pages) else 2
                        advanced = False
                        for retry_idx in range(max_turn_retries):
                            log(f"[Kindle] Page {page_idx} unchanged; retrying turn ({retry_idx + 1}/{max_turn_retries})...")
                            dismiss_kindle_dialogs(page)
                            if is_kindle_oops(page):
                                break
                            source = page.evaluate(KINDLE_PAGE_SOURCE)
                            page.keyboard.press(turn_key)
                            if source:
                                wait_for_kindle_turn(page, source)
                            else:
                                page.wait_for_timeout(1500)
                            if is_kindle_oops(page):
                                break
                            png_retry = page.screenshot(full_page=False, timeout=15000)
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
                                if is_kindle_oops(page):
                                    break
                                png_retry = page.screenshot(full_page=False, timeout=15000)
                                retry_hash = hashlib.sha256(png_retry).hexdigest()
                            if retry_hash != prev_hash:
                                png_bytes = png_retry
                                current_hash = retry_hash
                                advanced = True
                                break
                        if is_kindle_oops(page):
                            continue
                        if not advanced and (recovery_attempts or (
                            expected_total_pages is not None and last_kindle_page is not None
                            and last_kindle_page < expected_total_pages
                        )):
                            needs_recovery = True
                            continue
                    if prev_hash == current_hash:
                        log(f"[Browser] Page {page_idx} identical to previous; stopping pagination.")
                        break

                pdf_count_before = len(captured_pdfs)
                if kindle_mode:
                    if _find_kindle_page_box(page) is None:
                        _wait_for_kindle_reader(page, timeout_ms=8000)
                    if is_kindle_oops(page):
                        continue
                    k_res = capture_kindle_page_as_pdf_bytes(page)
                    if k_res is not None:
                        page_pdf_bytes, kindle_png = k_res
                        if page_idx == 1 and created_page:
                            cover_ratio = measure_cover_aspect_ratio(kindle_png)
                            if cover_ratio is not None:
                                target_h = max(900, min(1400, int(640 * cover_ratio + 150)))
                                try:
                                    page.set_viewport_size({"width": 800, "height": target_h})
                                    page.wait_for_timeout(800)
                                    k_res2 = capture_kindle_page_as_pdf_bytes(page)
                                    if k_res2 is not None:
                                        page_pdf_bytes, kindle_png = k_res2
                                except Exception:
                                    pass
                    elif last_kindle_image_hash is not None:
                        needs_recovery = True
                        continue
                    else:
                        page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                else:
                    page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                if kindle_mode and is_kindle_oops(page):
                    continue
                if is_pdf_visually_blank(page_pdf_bytes):
                    for retry in range(2):
                        log(f"[Browser] Page {page_idx} rendered blank; waiting and retrying ({retry + 1}/2)...")
                        page.wait_for_timeout(1500)
                        wait_for_page_ready(page, timeout_ms=10000)
                        if kindle_mode:
                            if is_kindle_oops(page):
                                break
                            retry_capture = capture_kindle_page_as_pdf_bytes(page)
                            if retry_capture is not None:
                                page_pdf_bytes, kindle_png = retry_capture
                            else:
                                continue
                        else:
                            page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                        if not is_pdf_visually_blank(page_pdf_bytes):
                            break
                    if kindle_mode and is_kindle_oops(page):
                        continue
                    if is_pdf_visually_blank(page_pdf_bytes):
                        if kindle_mode and last_kindle_image_hash is not None:
                            needs_recovery = True
                            continue
                        if page_idx == 1:
                            raise RuntimeError(
                                "ページの描画が空（白紙）のままだったためPDFを作成できませんでした。"
                                "ログイン状態やページ読み込みを確認して再実行してください。"
                            )
                        log(f"[Browser] Page {page_idx} remained blank after retry; stopping pagination.")
                        break
                current_pdf_hash = pdf_content_signature(page_pdf_bytes)
                if prev_pdf_hash == current_pdf_hash:
                    if kindle_mode:
                        for pdf_retry in range(2):
                            log(f"[Kindle] Page {page_idx} PDF content unchanged; retrying turn ({pdf_retry + 1}/2)...")
                            source = page.evaluate(KINDLE_PAGE_SOURCE)
                            page.keyboard.press(turn_key)
                            if source:
                                wait_for_kindle_turn(page, source)
                            else:
                                page.wait_for_timeout(2000)
                            if is_kindle_oops(page):
                                break
                            k_res_retry = capture_kindle_page_as_pdf_bytes(page)
                            if k_res_retry is not None:
                                page_pdf_bytes, kindle_png = k_res_retry
                            elif last_kindle_image_hash is None:
                                page_pdf_bytes = capture_page_as_pdf_bytes(page, cdp_session=cdp_session)
                            else:
                                continue
                            current_pdf_hash = pdf_content_signature(page_pdf_bytes)
                            if current_pdf_hash != prev_pdf_hash and not is_pdf_visually_blank(page_pdf_bytes):
                                break
                            current_pdf_hash = prev_pdf_hash
                    if kindle_mode and is_kindle_oops(page):
                        continue
                    if prev_pdf_hash == current_pdf_hash:
                        if kindle_mode and (recovery_attempts or (
                            expected_total_pages is not None and last_kindle_page is not None
                            and last_kindle_page < expected_total_pages
                        )):
                            needs_recovery = True
                            continue
                        if kindle_mode:
                            try:
                                stop_snippet = (page.inner_text("body") or "").replace("\n", " | ")[:200]
                                log(f"[Kindle] Stop context: {stop_snippet}")
                            except Exception:
                                pass
                        log(f"[Browser] Page {page_idx} PDF content identical to previous; stopping pagination.")
                        break
                if kindle_mode and is_kindle_oops(page):
                    continue
                prev_hash = current_hash
                prev_pdf_hash = current_pdf_hash
                if kindle_mode:
                    last_kindle_image_hash = hashlib.sha256(kindle_png).hexdigest() if kindle_png is not None else None
                    marker = get_kindle_position(page)
                    recovery_attempts = 0
                    k_total = get_kindle_total_pages(page)
                    if k_total is not None:
                        expected_total_pages = k_total
                    last_kindle_page = marker[1] if marker and (
                        marker[0] == "page" or expected_total_pages is None
                    ) else None
                    k_pct = get_kindle_progress_percent(page)
                    if k_pct is not None:
                        last_kindle_percent = k_pct
                    if kindle_checkpoint is not None:
                        kindle_checkpoint.append(page_idx, KindleCapture(
                            page_pdf_bytes, last_kindle_page, last_kindle_image_hash,
                            expected_total_pages, last_kindle_percent,
                            page.viewport_size["height"] if created_page and page.viewport_size else None,
                        ))
                collected_page_pdfs.append(page_pdf_bytes)
                log(f"[Browser] Captured page {page_idx} via PDF command ({len(page_pdf_bytes)} bytes)")

                if len(collected_page_pdfs) == max_pages:
                    break
                url_before_turn = page.url
                if kindle_mode:
                    source = page.evaluate(KINDLE_PAGE_SOURCE)
                    page.keyboard.press(turn_key)
                    if source:
                        # Let the next iteration handle unchanged pages and reader errors.
                        wait_for_kindle_turn(page, source)
                        continue
                    if is_kindle_oops(page):
                        continue
                else:
                    page.keyboard.press(turn_key)
                page.wait_for_timeout(500)
                wait_for_page_ready(page, timeout_ms=8000)

                # If a PDF response was triggered by the page turn, prefer collected PDFs if complete
                if len(captured_pdfs) > pdf_count_before:
                    log(f"[Browser] Intercepted PDF response after page turn {page_idx}.")

                # Fallback: if ArrowRight key did not change the URL or view, try clicking a right-arrow / next button
                after_key_hash = hashlib.sha256(page.screenshot(full_page=False, timeout=15000)).hexdigest()
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

            if not kindle_mode and captured_pdfs and len(captured_pdfs) >= len(collected_page_pdfs):
                written = merge_pdf_bytes_list(captured_pdfs, resolved_output)
            elif kindle_mode and kindle_checkpoint is not None:
                captures = kindle_checkpoint.load()
                if captures and kindle_at_last_page(captures[-1]):
                    return finish_kindle_at_end(start_url, resolved_output, turn_key, kindle_checkpoint, captures, result_details)
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=resolved_output.parent) as folder:
                    staged_output = Path(folder) / resolved_output.name
                    written = merge_pdf_bytes_list(collected_page_pdfs, staged_output)
                    check_kindle_completeness(
                        written, expected_total_pages, last_kindle_page, last_kindle_percent, max_pages,
                    )
                    verify_merged_kindle_pdf(collected_page_pdfs, staged_output, written)
                    staged_output.replace(resolved_output)
                    if result_details is not None:
                        result_details["verifiedPages"] = written
            else:
                written = merge_pdf_bytes_list(collected_page_pdfs, resolved_output)
            try:
                qc = verify_exported_pdf(
                    resolved_output,
                    expected_pages=expected_total_pages if last_kindle_page != expected_total_pages else None,
                    last_percent=last_kindle_percent,
                )
                log(f"[Verify] {qc['summary']}")
                log(f"QUALITY_CHECK={qc['summary']}")
            except Exception as qc_exc:
                log(f"[Verify] Skipped ({qc_exc})")
            if kindle_mode and kindle_checkpoint is not None:
                kindle_checkpoint.remove()
            if result_details is not None and expected_total_pages is not None:
                result_details["expectedPages"] = expected_total_pages
                if last_kindle_page == expected_total_pages:
                    result_details["lastPrintedPage"] = last_kindle_page
                    result_details["unverifiedGaps"] = 0
            return written, resolved_output
        finally:
            if created_page:
                try:
                    page.close()
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
    parser.add_argument("--no-browser", action="store_true", help="Disable Playwright browser fallback")
    parser.add_argument("--auto-name", action="store_true", help="Auto-name output as <Title>_<YYYYMMDD>.pdf when available")
    parser.add_argument("--kindle-checkpoint", type=Path, help="Save and resume captured Kindle pages")
    parser.add_argument("--result-json", type=Path, help="Write the output path and page count for the app")
    parser.add_argument("--inspect-kindle-total", action="store_true", help="Read only the Kindle book's total page count")
    args = parser.parse_args()

    if args.inspect_kindle_total:
        if args.result_json is None:
            parser.error("--inspect-kindle-total requires --result-json")
        try:
            total = inspect_kindle_total_pages(args.url)
            if total is None:
                raise RuntimeError("Kindle did not show a total page count.")
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
    checkpoint = KindleCheckpoint(args.kindle_checkpoint) if args.kindle_checkpoint else None
    try:
        if checkpoint is not None and not args.no_browser:
            if not is_http_url(args.url.strip()):
                raise ValueError("Invalid source URL")
            total_pages, final_path = fetch_via_browser(
                args.url.strip(), output_path,
                max_pages=max(1, args.max_pages), timeout=max(5, args.timeout),
                direction=args.direction, auto_name=args.auto_name, kindle_checkpoint=checkpoint,
                result_details=result_details,
            )
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
                        url=args.url.strip(),
                        output_path=output_path,
                        max_pages=max(1, args.max_pages),
                        timeout=max(5, args.timeout),
                        direction=args.direction,
                        auto_name=True,
                    ) if False else fetch_via_browser(
                        args.url.strip(),
                        output_path,
                        max_pages=max(1, args.max_pages),
                        timeout=max(5, args.timeout),
                        direction=args.direction,
                        auto_name=True,
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
    except Exception as exc:
        if args.result_json and isinstance(exc, IncompleteKindleCapture):
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
