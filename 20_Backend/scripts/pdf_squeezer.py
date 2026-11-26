#!/usr/bin/env python3
"""PDF Squeezer-style compressor for WebTPDF Mac using PyMuPDF."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops
import pymupdf as fitz


@dataclass(frozen=True)
class CompressionPreset:
    name: str
    dpi_threshold: int
    dpi_target: int
    quality: int


PRESETS: dict[str, CompressionPreset] = {
    "light": CompressionPreset(name="light", dpi_threshold=180, dpi_target=150, quality=85),
    "balanced": CompressionPreset(name="balanced", dpi_threshold=150, dpi_target=120, quality=75),
    "strong": CompressionPreset(name="strong", dpi_threshold=120, dpi_target=96, quality=60),
}


def format_bytes(size: int) -> str:
    """Format byte size into human-readable MB/KB."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


TRIM_PAD_PT = 4.0


def _find_horizontal_content_bands(im: Image.Image) -> list[tuple[int, int]]:
    """Return contiguous vertical [y0, y1] bands containing non-white pixels."""
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im, bg).convert("L")
    w, h = diff.size
    px = diff.load()
    row_has = []
    step_x = 2 if w > 200 else 1
    for y in range(h):
        has = False
        for x in range(0, w, step_x):
            if px[x, y] > 15:
                has = True
                break
        row_has.append(has)

    bands: list[tuple[int, int]] = []
    in_band = False
    start = 0
    gap = 0
    for y, has in enumerate(row_has):
        if has:
            if not in_band:
                start = y
                in_band = True
            gap = 0
        elif in_band:
            gap += 1
            if gap > 5:
                bands.append((start, y - gap))
                in_band = False
    if in_band:
        bands.append((start, h - 1 - gap))
    return bands


def _detect_document_chrome_y_bounds(doc: fitz.Document) -> tuple[float | None, float | None]:
    """Detect static Kindle Reader top/bottom chrome y-cutoffs only when present on both cover and interior pages."""
    if len(doc) < 2:
        return None, None

    sample_indices = [0, 1] + ([2] if len(doc) > 2 else [])
    top_candidates: list[tuple[int, int, int]] = []
    bot_candidates: list[tuple[int, int, int]] = []

    for idx in sample_indices:
        pg = doc[idx]
        pix = pg.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        bands = _find_horizontal_content_bands(im)
        if len(bands) < 2:
            return None, None

        h = pix.height
        # Top thin band separated from main body
        t0, t1 = bands[0]
        n0, _ = bands[1]
        if t1 <= h * 0.14 and (t1 - t0) <= 65 and (n0 - t1) >= 12:
            top_candidates.append((t0, t1, h))

        # Bottom thin bands separated from main body
        b_idx = len(bands) - 1
        while b_idx >= 1 and bands[b_idx][0] >= h * 0.80 and (bands[b_idx][1] - bands[b_idx][0]) <= 50:
            b_idx -= 1
        if b_idx < len(bands) - 1:
            prev_end = bands[b_idx][1]
            first_bot_y0 = bands[b_idx + 1][0]
            if (first_bot_y0 - prev_end) >= 15:
                bot_candidates.append((first_bot_y0, bands[-1][1], h))

    top_cut: float | None = None
    if len(top_candidates) == len(sample_indices):
        # Static top toolbar must sit at nearly the same y across cover and interior pages
        if max(t[1] for t in top_candidates) - min(t[1] for t in top_candidates) <= 15:
            top_cut = float(max(t[1] for t in top_candidates) + 2)

    bot_cut: float | None = None
    if len(bot_candidates) == len(sample_indices):
        # Bottom bar offset from bottom edge (h - y0) is consistent across pages
        offsets = [b[2] - b[0] for b in bot_candidates]
        if max(offsets) - min(offsets) <= 25:
            # Store as distance from bottom edge? Return per-page check via ratio or absolute when same height
            bot_cut = float(max(offsets) + 2)

    return top_cut, bot_cut


def _measure_page_content_rect(
    page: fitz.Page,
    top_chrome_cut: float | None = None,
    bot_chrome_offset: float | None = None,
) -> fitz.Rect | None:
    """Measure a page's non-white content rect (at 1x point scale), excluding verified static chrome."""
    pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
    im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    y0 = int(top_chrome_cut) if top_chrome_cut is not None else 0
    y1 = int(pix.height - bot_chrome_offset) if bot_chrome_offset is not None else pix.height
    y0 = max(0, min(y0, pix.height - 20))
    y1 = max(y0 + 20, min(y1, pix.height))

    cropped_y = im.crop((0, y0, pix.width, y1))
    bg = Image.new("RGB", cropped_y.size, (255, 255, 255))
    bbox = ImageChops.difference(cropped_y, bg).getbbox()
    if not bbox:
        return None

    x0 = max(0.0, float(bbox[0]) - TRIM_PAD_PT)
    x1 = min(float(pix.width), float(bbox[2]) + TRIM_PAD_PT)
    top = max(0.0, float(y0 + bbox[1]) - TRIM_PAD_PT)
    bot = min(float(pix.height), float(y0 + bbox[3]) + TRIM_PAD_PT)

    # Map pixel rect back to page mediabox coordinates
    mb = page.mediabox
    sx = mb.width / float(pix.width) if pix.width else 1.0
    sy = mb.height / float(pix.height) if pix.height else 1.0
    return fitz.Rect(mb.x0 + x0 * sx, mb.y0 + top * sy, mb.x0 + x1 * sx, mb.y0 + bot * sy)


def trim_pdf_to_cover(doc: fitz.Document) -> int:
    """Trim margins and verified static reader chrome across all pages using cover dimensions as baseline."""
    if len(doc) == 0:
        return 0

    top_cut, bot_offset = _detect_document_chrome_y_bounds(doc)
    cover_rect = _measure_page_content_rect(doc[0], top_cut, bot_offset)
    cover_w = cover_rect.width if cover_rect and cover_rect.width > 50 else None
    cover_h = cover_rect.height if cover_rect and cover_rect.height > 50 else None

    trimmed_pages = 0
    for idx in range(len(doc)):
        page = doc[idx]
        mb = page.mediabox
        content = _measure_page_content_rect(page, top_cut, bot_offset)
        if content is None or content.width <= 20 or content.height <= 20:
            continue

        target = fitz.Rect(content)
        if cover_w is not None and cover_h is not None and idx > 0:
            # Expand symmetrically to at least cover size when it fits inside the page mediabox
            if target.width < cover_w:
                cx = (target.x0 + target.x1) / 2.0
                target.x0 = max(mb.x0, cx - cover_w / 2.0)
                target.x1 = min(mb.x1, cx + cover_w / 2.0)
            if target.height < cover_h:
                cy = (target.y0 + target.y1) / 2.0
                target.y0 = max(mb.y0, cy - cover_h / 2.0)
                target.y1 = min(mb.y1, cy + cover_h / 2.0)

        target &= mb
        if target.width > 20 and target.height > 20:
            page.set_cropbox(target)
            trimmed_pages += 1
    return trimmed_pages


def compress_pdf(
    input_path: Path,
    output_path: Path,
    preset: CompressionPreset,
    in_place: bool = False,
    trim_to_cover: bool = False,
    no_compress: bool = False,
) -> tuple[int, int, bool]:
    """Compress and/or trim a single PDF and return (before_bytes, after_bytes, replaced_or_written)."""
    before_bytes = input_path.stat().st_size
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=str(output_path.parent),
        prefix=f".{input_path.stem}_",
        suffix=".pdf",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with fitz.open(str(input_path)) as doc:
            if trim_to_cover:
                trim_pdf_to_cover(doc)
            if not no_compress:
                doc.rewrite_images(
                    dpi_threshold=preset.dpi_threshold,
                    dpi_target=preset.dpi_target,
                    quality=preset.quality,
                    lossy=True,
                    lossless=True,
                    bitonal=True,
                    color=True,
                    gray=True,
                )
            doc.save(
                str(tmp_path),
                garbage=4,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
                clean=True,
                use_objstms=1,
            )

        after_bytes = tmp_path.stat().st_size
        if in_place and not trim_to_cover and after_bytes >= before_bytes:
            tmp_path.unlink(missing_ok=True)
            return before_bytes, before_bytes, False

        tmp_path.replace(output_path)
        return before_bytes, after_bytes, True
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def save_pdf_page_range(src_doc: fitz.Document, start_idx: int, end_idx: int, out_path: Path) -> int:
    """Write inclusive [start_idx, end_idx] page range from src_doc and return file size in bytes."""
    chunk = fitz.open()
    try:
        chunk.insert_pdf(src_doc, from_page=start_idx, to_page=end_idx)
        chunk.save(
            str(out_path),
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            clean=True,
            use_objstms=1,
        )
    finally:
        chunk.close()
    return out_path.stat().st_size


def split_pdf_by_size(
    compressed_path: Path,
    output_folder: Path,
    base_stem: str,
    max_bytes: int,
) -> list[tuple[Path, int, int, int]]:
    """Split compressed_path into <= max_bytes PDFs inside output_folder without touching the source."""
    output_folder.mkdir(parents=True, exist_ok=True)
    # Remove any old part files in this dedicated split folder before writing new parts
    for existing in output_folder.glob(f"{base_stem}_part*.pdf"):
        existing.unlink(missing_ok=True)

    results: list[tuple[Path, int, int, int]] = []
    with fitz.open(str(compressed_path)) as doc:
        total_pages = len(doc)
        if total_pages == 0:
            raise RuntimeError(f"PDF has 0 pages: {compressed_path}")

        total_size = compressed_path.stat().st_size
        if total_size <= max_bytes:
            dst = output_folder / f"{base_stem}_part01.pdf"
            shutil.copy2(compressed_path, dst)
            return [(dst, 1, total_pages, dst.stat().st_size)]

        avg_page_bytes = max(1, total_size // total_pages)
        safe_limit = max(64 * 1024, int(max_bytes * 0.97))

        parts_ranges: list[tuple[int, int, Path]] = []
        with tempfile.TemporaryDirectory(dir=str(output_folder), prefix=".split_") as tmp_dir:
            tmp_root = Path(tmp_dir)
            start = 0
            part_num = 0
            while start < total_pages:
                part_num += 1
                est_count = max(1, int((safe_limit / avg_page_bytes) * 0.9))
                high = min(total_pages - 1, start + est_count - 1)

                # Expand upper bound exponentially if est_count still fits under safe_limit
                probe_path = tmp_root / f"probe_{part_num}.pdf"
                while high < total_pages - 1:
                    sz = save_pdf_page_range(doc, start, high, probe_path)
                    if sz > safe_limit:
                        break
                    step = max(1, (high - start + 1))
                    next_high = min(total_pages - 1, high + step)
                    if next_high == high:
                        break
                    high = next_high

                # Binary search largest end in [start, high] whose saved size <= safe_limit
                lo = start
                hi = high
                best_end = start
                best_tmp = tmp_root / f"part_{part_num}.pdf"

                while lo <= hi:
                    mid = (lo + hi) // 2
                    cand_path = tmp_root / f"cand_{part_num}_{mid}.pdf"
                    sz = save_pdf_page_range(doc, start, mid, cand_path)
                    if sz <= safe_limit:
                        best_end = mid
                        cand_path.replace(best_tmp)
                        lo = mid + 1
                    else:
                        cand_path.unlink(missing_ok=True)
                        hi = mid - 1

                if not best_tmp.exists():
                    # Even a single page exceeded safe_limit; try stronger image recompression for that single page
                    sz = save_pdf_page_range(doc, start, start, best_tmp)
                    if sz > max_bytes:
                        strong_tmp = tmp_root / f"strong_{part_num}.pdf"
                        with fitz.open(str(best_tmp)) as single_doc:
                            single_doc.rewrite_images(
                                dpi_threshold=96,
                                dpi_target=72,
                                quality=45,
                                lossy=True,
                                lossless=True,
                                bitonal=True,
                                color=True,
                                gray=True,
                            )
                            single_doc.save(
                                str(strong_tmp),
                                garbage=4,
                                deflate=True,
                                deflate_images=True,
                                deflate_fonts=True,
                                clean=True,
                                use_objstms=1,
                            )
                        if strong_tmp.stat().st_size < sz:
                            strong_tmp.replace(best_tmp)
                        else:
                            strong_tmp.unlink(missing_ok=True)

                parts_ranges.append((start, best_end, best_tmp))
                start = best_end + 1

            pad = 3 if len(parts_ranges) >= 100 else 2
            for idx, (s, e, tmp_file) in enumerate(parts_ranges, start=1):
                final_path = output_folder / f"{base_stem}_part{idx:0{pad}d}.pdf"
                tmp_file.replace(final_path)
                results.append((final_path, s + 1, e + 1, final_path.stat().st_size))

    return results


def collect_pdfs(inputs: list[Path], recursive: bool) -> list[Path]:
    """Expand file and directory inputs into a sorted list of unique PDF paths."""
    seen: set[Path] = set()
    result: list[Path] = []
    for item in inputs:
        resolved = item.expanduser().resolve()
        if resolved.is_dir():
            iterator = resolved.rglob("*.pdf") if recursive else resolved.glob("*.pdf")
            for p in sorted(iterator):
                if p.is_file() and not p.stem.endswith("_compressed") and p not in seen:
                    seen.add(p)
                    result.append(p)
        elif resolved.is_file() and resolved.suffix.lower() == ".pdf":
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Compress PDF files using PyMuPDF.")
    parser.add_argument("--input", dest="inputs", action="append", required=True, help="PDF file or folder path (repeatable)")
    parser.add_argument("--output-dir", help="Optional output directory (ignored when --in-place is set)")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), default="balanced", help="Compression strength preset")
    parser.add_argument("--in-place", action="store_true", help="Overwrite original PDF when smaller")
    parser.add_argument("--recursive", action="store_true", help="Scan subdirectories when input is a folder")
    parser.add_argument("--max-size-mb", type=float, default=0.0, help="Split compressed PDF into <= N MB parts inside a dedicated folder")
    parser.add_argument("--trim-to-cover", action="store_true", help="Trim margins and Kindle Reader chrome using cover dimensions")
    parser.add_argument("--no-compress", action="store_true", help="Skip image recompression (useful when only trimming)")
    args = parser.parse_args()

    preset = PRESETS[args.preset]
    max_bytes = int(args.max_size_mb * 1024 * 1024) if args.max_size_mb and args.max_size_mb > 0 else 0
    in_place = args.in_place and max_bytes == 0
    trim_to_cover = bool(args.trim_to_cover)
    no_compress = bool(args.no_compress)
    pdfs = collect_pdfs([Path(p) for p in args.inputs], recursive=args.recursive)
    if not pdfs:
        print("Error: No PDF files found.", file=sys.stderr, flush=True)
        return 1

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
    total_before = 0
    total_after = 0
    total_count = len(pdfs)

    mode_desc = f"split<={args.max_size_mb:g}MB" if max_bytes > 0 else f"in_place={in_place}"
    trim_desc = ", trim_to_cover=True" if trim_to_cover else ""
    comp_desc = ", no_compress=True" if no_compress else ""
    print(f"[Squeezer] Starting ({total_count} file(s), preset={preset.name}, {mode_desc}{trim_desc}{comp_desc})", flush=True)

    for idx, src in enumerate(pdfs, start=1):
        if max_bytes > 0:
            split_folder = (output_dir if output_dir is not None else src.parent) / f"{src.stem}_split"
            try:
                with tempfile.NamedTemporaryFile(
                    dir=str(split_folder.parent if split_folder.parent.exists() else src.parent),
                    prefix=f".compressed_{src.stem}_",
                    suffix=".pdf",
                    delete=False,
                ) as tmp:
                    tmp_compressed = Path(tmp.name)
                try:
                    before, compressed_bytes, _ = compress_pdf(
                        src,
                        tmp_compressed,
                        preset=preset,
                        in_place=False,
                        trim_to_cover=trim_to_cover,
                        no_compress=no_compress,
                    )
                    parts = split_pdf_by_size(tmp_compressed, split_folder, src.stem, max_bytes=max_bytes)
                finally:
                    tmp_compressed.unlink(missing_ok=True)

                parts_total = sum(p[3] for p in parts)
                total_before += before
                total_after += parts_total
                saved_pct = ((before - compressed_bytes) / before * 100.0) if before > 0 else 0.0
                print(
                    f"[{idx}/{total_count}] SPLIT OK: {src.name} | {format_bytes(before)} -> {format_bytes(compressed_bytes)} (-{saved_pct:.1f}%) | {len(parts)} part(s) -> {split_folder}",
                    flush=True,
                )
                for part_path, p_start, p_end, p_bytes in parts:
                    warn = " [WARN: single page exceeds limit]" if p_bytes > max_bytes else ""
                    print(
                        f"    - {part_path.name} (p.{p_start}-{p_end}): {format_bytes(p_bytes)}{warn}",
                        flush=True,
                    )
                print(f"SPLIT_OUTPUT_DIR={split_folder}", flush=True)
            except Exception as exc:
                print(f"[{idx}/{total_count}] ERROR: {src.name} | {exc}", file=sys.stderr, flush=True)
            continue

        if in_place:
            dst = src
        elif output_dir is not None:
            dst = output_dir / src.name
        elif trim_to_cover and no_compress:
            dst = src.with_name(f"{src.stem}_trimmed.pdf")
        else:
            dst = src.with_name(f"{src.stem}_compressed.pdf")

        try:
            before, after, written = compress_pdf(
                src,
                dst,
                preset=preset,
                in_place=in_place,
                trim_to_cover=trim_to_cover,
                no_compress=no_compress,
            )
            total_before += before
            total_after += after
            saved_pct = ((before - after) / before * 100.0) if before > 0 else 0.0
            status = "OK" if written else "SKIP (already optimal)"
            print(
                f"[{idx}/{total_count}] {status}: {src.name} | {format_bytes(before)} -> {format_bytes(after)} (-{saved_pct:.1f}%)",
                flush=True,
            )
        except Exception as exc:
            print(f"[{idx}/{total_count}] ERROR: {src.name} | {exc}", file=sys.stderr, flush=True)

    total_saved = total_before - total_after
    total_pct = (total_saved / total_before * 100.0) if total_before > 0 else 0.0
    print(
        f"[Squeezer] Completed: {format_bytes(total_before)} -> {format_bytes(total_after)} (Saved {format_bytes(total_saved)}, -{total_pct:.1f}%)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
