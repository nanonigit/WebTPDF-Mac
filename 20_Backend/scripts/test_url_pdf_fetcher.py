#!/usr/bin/env python3
"""Unit tests for url_pdf_fetcher.py using a local HTTP test server."""

from __future__ import annotations

import functools
import http.server
import io
import json
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import url_pdf_fetcher as fetcher  # noqa: E402


def make_sample_pdf_bytes(page_count: int) -> bytes:
    """Create a valid blank PDF with the requested number of pages."""
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with suppressed stderr logging."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class UrlPdfFetcherTests(unittest.TestCase):
    """Test direct PDF download, paginated HTML PDF crawling, and error handling."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        # Create sample PDFs (2 pages, 3 pages, 1 page)
        (self.root / "doc1.pdf").write_bytes(make_sample_pdf_bytes(2))
        (self.root / "doc2.pdf").write_bytes(make_sample_pdf_bytes(3))
        (self.root / "doc3.pdf").write_bytes(make_sample_pdf_bytes(1))

        # Create paginated HTML pages
        (self.root / "page1.html").write_text(
            """
            <html><body>
              <a href="doc1.pdf">Doc 1</a>
              <a href="/page2.html">次へ</a>
            </body></html>
            """,
            encoding="utf-8",
        )
        (self.root / "page2.html").write_text(
            """
            <html><body>
              <a href="doc1.pdf">Doc 1 Duplicate</a>
              <a href="doc2.pdf">Doc 2</a>
              <a rel="next" href="page3.html">Next</a>
            </body></html>
            """,
            encoding="utf-8",
        )
        (self.root / "page3.html").write_text(
            """
            <html><body>
              <iframe src="doc3.pdf"></iframe>
            </body></html>
            """,
            encoding="utf-8",
        )
        (self.root / "viewer.html").write_text(
            """
            <html><body>
              <div id="spinner">Hang tight. This'll take just a sec.</div>
              <h1 id="title" style="display:none">Slide 1</h1>
              <script>
                let page = 1;
                function showPage(n) {
                  document.getElementById('spinner').style.display = 'block';
                  document.getElementById('title').style.display = 'none';
                  setTimeout(() => {
                    document.getElementById('spinner').style.display = 'none';
                    document.getElementById('title').textContent = 'Slide ' + n;
                    document.getElementById('title').style.display = 'block';
                  }, 700);
                }
                showPage(1);
                document.addEventListener('keydown', (e) => {
                  if (e.key === 'ArrowRight' && page < 3) {
                    page += 1;
                    showPage(page);
                  }
                });
              </script>
            </body></html>
            """,
            encoding="utf-8",
        )

        (self.root / "viewer_left.html").write_text(
            """
            <html><body>
              <h1 id="title">RTL Page 1</h1>
              <script>
                let page = 1;
                document.addEventListener('keydown', (e) => {
                  if (e.key === 'ArrowLeft' && page < 2) {
                    page += 1;
                    document.getElementById('title').textContent = 'RTL Page ' + page;
                  }
                });
              </script>
            </body></html>
            """,
            encoding="utf-8",
        )
        paragraphs = "\n".join(
            f"<p>LineMarker_{i:03d}: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt.</p>"
            for i in range(1, 65)
        )
        (self.root / "long_chapter.html").write_text(
            f"""
            <html><head><style>
              @page {{ margin: 0.05in; }}
              header {{ position: sticky; top: 0; height: 60px; background: #eee; }}
            </style></head><body>
              <header>Sticky Navbar</header>
              <main>{paragraphs}</main>
            </body></html>
            """,
            encoding="utf-8",
        )
        (self.root / "fixed_layout.html").write_text(
            """
            <html><head><style>
              .pf { width: 420px; height: 595px; position: relative; }
              #pdf-main { transform: scale(1.5); transform-origin: 0 0; }
            </style></head><body>
              <div id="sbo-rt-content">
                <div id="pdf-main">
                  <div class="pdf-page"><div class="pf">FixedPage_001</div></div>
                  <div class="pdf-page"><div class="pf">FixedPage_002</div></div>
                  <div class="pdf-page"><div class="pf">FixedPage_003</div></div>
                </div>
              </div>
            </body></html>
            """,
            encoding="utf-8",
        )
        (self.root / "sidebar_layout.html").write_text(
            """
            <html><body>
              <header>TOP_BANNER_SHOULD_HIDE</header>
              <div style="display:flex;">
                <main id="sbo-rt-content" style="width:300px;">
                  <section class="_contentContainer_test _sidebarIsOpen_test">
                    <div data-testid="alternativeFormatCallout">Other formats available Audiobook</div>
                    <h1>Main Chapter Title</h1>
                    <p>READER_BODY_CONTENT_LINE</p>
                  </section>
                </main>
                <aside class="sidebar">SIDEBAR_EXPLANATION_SHOULD_HIDE</aside>
              </div>
            </body></html>
            """,
            encoding="utf-8",
        )
        (self.root / "blank_page.html").write_text(
            """
            <html><body><header></header><main id="sbo-rt-content"></main></body></html>
            """,
            encoding="utf-8",
        )
        handler = functools.partial(QuietHandler, directory=str(self.root))
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_kindle_redownload_merges_saved_pages_without_gaps(self) -> None:
        import fitz

        reader = self.root / "kindle.html"
        html = """
            <img id="page" width="200" height="260"><p id="position"></p>
            <button id="back" hidden>Back to Library</button>
            <button data-asin="TEST" hidden>Open book</button>
            <button aria-label="Table of Contents" id="toc-toggle">☰</button>
            <section hidden id="toc">
              <h2>Table of Contents</h2>
              <button data-page="23" aria-current="location">Chapter A</button>
              <button data-page="27">Chapter B</button>
            </section>
            <script>
              let current = 1;
              let objectURL;
              let usedToc = false;
              const image = document.querySelector('#page');
              const position = document.querySelector('#position');
              const back = document.querySelector('#back');
              const book = document.querySelector('[data-asin]');
              function render() {
                const canvas = document.createElement('canvas');
                canvas.width = 200;
                canvas.height = 260;
                const ctx = canvas.getContext('2d');
                ctx.fillStyle = 'rgb(' + current * 8 + ',60,120)';
                ctx.fillRect(0, 0, 200, 260);
                canvas.toBlob((blob) => {
                  if (objectURL) URL.revokeObjectURL(objectURL);
                  objectURL = URL.createObjectURL(blob);
                  image.src = objectURL;
                  image.hidden = false;
                  position.textContent = 'Page ' + current + ' of 28';
                });
              }
              back.onclick = () => {
                back.hidden = true;
                position.textContent = '';
                book.hidden = false;
              };
              book.onclick = () => {
                book.hidden = true;
                current = 23;
                render();
              };
              const toc = document.querySelector('#toc');
              document.querySelector('#toc-toggle').onclick = () => {
                toc.hidden = !toc.hidden;
              };
              for (const chapter of toc.querySelectorAll('[data-page]')) {
                chapter.onclick = () => {
                  usedToc = true;
                  current = Number(chapter.dataset.page);
                  toc.hidden = true;
                  render();
                };
              }
              document.addEventListener('keydown', (event) => {
                if (!back.hidden || !book.hidden) return;
                const next = current + (event.key === 'ArrowRight' ? 1 : -1);
                if (next < 1 || next > 28) return;
                if (FAIL_CONDITION) {
                  image.hidden = false; // The old page is still behind the dialog.
                  position.textContent = 'Oops... Something Went Wrong';
                  back.hidden = false;
                  return;
                }
                current = next;
                render();
              });
              render();
            </script>
        """
        reader.write_text(html.replace("FAIL_CONDITION", "next === 25"), encoding="utf-8")
        url = f"http://127.0.0.1:{self.port}/kindle.html?asin=TEST"
        output = self.root / "resumed.pdf"
        checkpoint = fetcher.KindleCheckpoint(self.root / "checkpoint.sqlite3")
        with patch.object(fetcher, "resolve_cdp_url", return_value=None), patch.object(
            fetcher, "is_kindle_cloud_reader", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "取得済み: 24"):
                fetcher.fetch_via_browser(url, output, 28, 10, kindle_checkpoint=checkpoint)
            self.assertEqual(len(checkpoint.load()), 24)
            self.assertFalse(output.exists())

            report = self.root / "result.json"
            args = [
                "url_pdf_fetcher", "--url", url, "--output", str(output),
                "--max-pages", "24", "--kindle-checkpoint", str(checkpoint.path),
                "--result-json", str(report),
            ]
            with patch.object(sys, "argv", args):
                self.assertEqual(fetcher.main(), 1)
            status = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual((status["pageCount"], status["expectedPages"]), (24, 28))
            self.assertIn("上限", status["message"])
            self.assertFalse(output.exists())
            self.assertEqual(len(checkpoint.load()), 24)

            # The next chapter opens at 27 but page turns into 26 only work
            # after visiting it. Recovery must go back to the verified anchor.
            reader.write_text(
                html.replace("FAIL_CONDITION", "next === 26 && !usedToc"), encoding="utf-8"
            )
            args[args.index("--max-pages") + 1] = "28"
            with patch.object(sys, "argv", args), patch.object(
                fetcher, "jump_to_kindle_chapter", wraps=fetcher.jump_to_kindle_chapter
            ) as jump, patch.object(fetcher, "recover_kindle_oops", wraps=fetcher.recover_kindle_oops) as recover:
                self.assertEqual(fetcher.main(), 0)
                self.assertEqual(jump.call_args.args[1], 1)
                same_tab = recover.call_args_list[0].args[0]
                self.assertTrue(recover.call_args.kwargs["fresh_tab"])
                self.assertIsNot(recover.call_args.args[0], same_tab)
                for attempt in recover.call_args_list[-4:-1]:
                    self.assertTrue(attempt.kwargs["use_location"])
                    self.assertIsNone(attempt.kwargs["toc_offset"])
                self.assertFalse(recover.call_args.kwargs["use_location"])
                self.assertEqual(recover.call_args.kwargs["toc_offset"], 1)
            status = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual((status["pageCount"], status["expectedPages"]), (28, 28))
        self.assertEqual(Path(status["outputPath"]), output.resolve())
        self.assertFalse(checkpoint.path.exists())
        with fitz.open(output) as pdf:
            for number, page in enumerate(pdf, 1):
                pixels = page.get_pixmap()
                self.assertAlmostEqual(pixels.pixel(pixels.width // 2, pixels.height // 2)[0], number * 8, delta=3)

    def test_kindle_gap_only_inserts_views_between_matched_anchors(self) -> None:
        import hashlib
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                    <img width="180" height="210"><p></p>
                    <script>
                      const img = document.querySelector('img');
                      let pageNumber = 1;
                      window.skipMiddle = false;
                      window.render = (n) => {
                        pageNumber = n;
                        const canvas = document.createElement('canvas');
                        canvas.width = 180;
                        canvas.height = 210;
                        const ctx = canvas.getContext('2d');
                        ctx.fillStyle = 'rgb(' + n * 60 + ',60,120)';
                        ctx.fillRect(0, 0, 180, 210);
                        canvas.toBlob((blob) => {
                          img.src = URL.createObjectURL(blob);
                          document.querySelector('p').textContent = 'Page ' + n + ' of 3';
                        });
                      };
                      window.addEventListener("keydown", (event) => {
                        if (event.key === 'ArrowRight' && pageNumber < 3)
                          render(window.skipMiddle ? 3 : pageNumber + 1);
                      });
                      render(1);
                    </script>
                """)

                def ready() -> None:
                    page.wait_for_function("() => document.querySelector('img').complete && document.querySelector('img').naturalWidth")

                def saved(number: int):
                    page.evaluate("(n) => render(n)", number)
                    page.get_by_text(f"Page {number} of 3", exact=True).wait_for()
                    ready()
                    pdf, png = fetcher.capture_kindle_page_as_pdf_bytes(page)
                    return fetcher.KindleCapture(pdf, number, hashlib.sha256(png).hexdigest(), 3, None, None)

                left, right = saved(1), saved(3)
                saved(1)
                repaired = fetcher.verify_kindle_gap(page, left, right, "ArrowRight")
                self.assertIsNotNone(repaired)
                self.assertEqual(len(PdfReader(io.BytesIO(repaired)).pages), 1)
                page.evaluate("window.skipMiddle = true")
                saved(1)
                self.assertEqual(fetcher.verify_kindle_gap(page, left, right, "ArrowRight"), b"")
            finally:
                browser.close()

    def test_kindle_end_finalizes_saved_pdf_and_checks_repaired_order(self) -> None:
        from PIL import Image
        import fitz

        def tile(value: int) -> bytes:
            output = io.BytesIO()
            Image.new("RGB", (100, 120), (value, 20, 40)).save(output, "PDF")
            return output.getvalue()

        checkpoint = fetcher.KindleCheckpoint(self.root / "finished.sqlite3")
        source = [tile(value) for value in (40, 120, 160)]
        for ordinal, (position, pdf) in enumerate(zip((1, 3, 4), source), 1):
            checkpoint.append(ordinal, fetcher.KindleCapture(
                pdf, position, "fixture", 4, 100 if position == 4 else 0, 1114,
            ))
        url = "https://read.amazon.co.jp/?asin=TEST"
        output = self.root / "merged.pdf"
        details = {}
        with (
            patch.object(fetcher, "resolve_cdp_url", return_value=None),
            patch.object(fetcher, "log") as log,
        ):
            count, path = fetcher.fetch_via_browser(
                url, output, 100, 10, kindle_checkpoint=checkpoint, result_details=details,
            )
        self.assertEqual([message for (message,), _ in log.call_args_list
                          if message.startswith("KINDLE_UNVERIFIED_GAPS=")],
                         ["KINDLE_UNVERIFIED_GAPS=1"])
        self.assertEqual((count, path), (3, output))
        self.assertEqual(details, {
            "expectedPages": 4, "lastPrintedPage": 4, "unverifiedGaps": 1, "verifiedPages": 3,
        })
        self.assertTrue(checkpoint.path.exists())

        # A successful retry publishes the updated count and replaces the PDF
        # at the same path after verifying its pages.
        inserted = tile(80)
        second_output = self.root / "another-name.pdf"
        with (
            patch("playwright.sync_api.sync_playwright"),
            patch.object(fetcher, "resolve_cdp_url", return_value="http://localhost"),
            patch.object(fetcher, "_wait_for_kindle_reader", return_value=True),
            patch.object(fetcher, "verify_kindle_gap", return_value=inserted),
            patch.object(fetcher, "log") as log,
        ):
            count, path = fetcher.fetch_via_browser(
                url, second_output, 100, 10, kindle_checkpoint=checkpoint, result_details=details,
            )
        self.assertEqual([message for (message,), _ in log.call_args_list
                          if message.startswith("KINDLE_UNVERIFIED_GAPS=")],
                         ["KINDLE_UNVERIFIED_GAPS=1", "KINDLE_UNVERIFIED_GAPS=0"])
        self.assertEqual((count, path), (4, output))
        self.assertEqual(details["unverifiedGaps"], 0)
        self.assertEqual(details["verifiedPages"], 4)
        self.assertFalse(checkpoint.path.exists())
        self.assertFalse(second_output.exists())
        with fitz.open(output) as final:
            for value, page in zip((40, 80, 120, 160), final):
                pixels = page.get_pixmap()
                self.assertAlmostEqual(pixels.pixel(pixels.width // 2, pixels.height // 2)[0], value, delta=3)
        with self.assertRaisesRegex(RuntimeError, "1 ページ目"):
            fetcher.verify_merged_kindle_pdf([inserted, *source], output, 4)

    def test_kindle_go_to_location_uses_the_rendered_page_to_seek(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("""
                    <button id="more" aria-label="More options">⋮</button>
                    <button id="location-menu" hidden>Go to Location</button>
                    <button id="page-menu" hidden>Go to Page</button>
                    <section id="dialog" hidden>
                      <h2>Go to Location</h2>
                      <label>Go to location <input type="number" placeholder="location"> of 499</label>
                      <button id="go">Go</button>
                      <button id="cancel">Cancel</button>
                    </section>
                    <img width="180" height="180"><p></p>
                    <script>
                      const byId = (id) => document.getElementById(id);
                      const input = document.querySelector('input');
                      const image = document.querySelector('img');
                      window.visitedLocations = [];
                      window.visitedPages = [];
                      window.pageMenuAvailable = false;
                      let targetIsPage = false;
                      function render(position) {
                        const canvas = document.createElement('canvas');
                        canvas.toBlob((blob) => {
                          image.src = URL.createObjectURL(blob);
                          document.querySelector('p').textContent =
                            'Page ' + (1 + Math.floor(position / 10)) + ' of 50';
                        });
                      }
                      byId('more').onclick = () => {
                        byId('location-menu').hidden = false;
                        byId('page-menu').hidden = !window.pageMenuAvailable;
                      };
                      function openDialog(usePage) {
                        targetIsPage = usePage;
                        byId('location-menu').hidden = true;
                        byId('page-menu').hidden = true;
                        document.querySelector('h2').textContent = usePage ? 'Go to Page' : 'Go to Location';
                        byId('dialog').hidden = false;
                      }
                      byId('location-menu').onclick = () => openDialog(false);
                      byId('page-menu').onclick = () => openDialog(true);
                      byId('go').onclick = () => {
                        const number = Number(input.value);
                        (targetIsPage ? window.visitedPages : window.visitedLocations).push(number);
                        byId('dialog').hidden = true;
                        render(targetIsPage ? (number - 1) * 10 : number);
                      };
                      byId('cancel').onclick = () => byId('dialog').hidden = true;
                      render(10);
                    </script>
                """)
                page.get_by_text("Page 2 of 50", exact=True).wait_for()
                fetcher.seek_kindle_position(page, 24, target_is_printed_page=True)
                kind, position = fetcher.get_kindle_position(page)
                self.assertEqual(kind, "page")
                self.assertLessEqual(abs(position - 24), 1)
                entered = page.evaluate("window.visitedLocations")
                self.assertGreater(len(entered), 1)
                self.assertNotIn(24, entered)

                # The live reader's cover shows Location even when the saved
                # checkpoint is a printed page. The first probe reveals pages.
                page.locator("p").evaluate("(footer) => footer.textContent = 'Location 4 of 499 ● 0%'")
                self.assertTrue(fetcher.seek_kindle_position(page, 24, True, 50))
                self.assertEqual(fetcher.get_kindle_position(page), ("page", 24))
                entered = page.evaluate("window.visitedLocations")
                self.assertGreater(entered[-1], 24)

                # Typing is still attempted if a reopen already shows the saved
                # page number. The direct page field wins when both are offered.
                page.evaluate("window.pageMenuAvailable = true")
                position = 24
                self.assertTrue(fetcher.seek_kindle_position(page, position, target_is_printed_page=True))
                self.assertEqual(page.evaluate("window.visitedPages"), [position])
                self.assertEqual(page.evaluate("window.visitedLocations"), entered)
            finally:
                browser.close()

    def test_kindle_checkpoint_image_tolerates_encoding_but_rejects_other_content(self) -> None:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (400, 600), "white")
        draw = ImageDraw.Draw(image)
        for y in range(120, 440, 24):
            draw.rectangle((50, y, 310, y + 8), fill="black")
        padded = Image.new("RGB", (456, 684), "white")
        padded.paste(image, (28, 42))
        checkpoint = io.BytesIO()
        padded.save(checkpoint, "PDF")
        frame = io.BytesIO()
        image.save(frame, "PNG")
        self.assertTrue(fetcher.matches_saved_kindle_image(frame.getvalue(), checkpoint.getvalue()))
        slightly_darker = io.BytesIO()
        Image.eval(image, lambda value: max(0, value - 1)).save(slightly_darker, "PNG")
        self.assertFalse(fetcher.matches_saved_kindle_image(slightly_darker.getvalue(), checkpoint.getvalue()))
        self.assertTrue(fetcher.matches_saved_kindle_image(
            slightly_darker.getvalue(), checkpoint.getvalue(), same_printed_page=True,
        ))
        draw.rectangle((50, 455, 290, 470), fill="black")
        different = io.BytesIO()
        image.save(different, "PNG")
        self.assertFalse(fetcher.matches_saved_kindle_image(different.getvalue(), checkpoint.getvalue()))
        self.assertFalse(fetcher.matches_saved_kindle_image(
            different.getvalue(), checkpoint.getvalue(), same_printed_page=True,
        ))

    def test_kindle_library_reopens_unique_exact_title(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("https://read.amazon.co.jp/**", lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
                        <button id="back">Back to Library</button>
                        <button hidden id="other">Example Book, second edition</button>
                        <button hidden id="book">Example Book</button>
                        <script>
                          back.onclick = () => {
                            back.hidden = true;
                            book.hidden = false;
                            other.hidden = false;
                          };
                          book.onclick = () => {
                            const image = new Image(180, 180);
                            const canvas = document.createElement('canvas');
                            canvas.toBlob(blob => {
                              image.src = URL.createObjectURL(blob);
                              document.body.append(image);
                            });
                          };
                        </script>
                    """,
                ))
                url = "https://read.amazon.co.jp/?asin=TEST"
                page.goto(url)
                self.assertTrue(fetcher.reopen_kindle_from_library(page, url, "Example Book"))
            finally:
                browser.close()

    def test_canceled_kindle_capture_reports_saved_progress(self) -> None:
        output = self.root / "canceled.pdf"
        checkpoint = fetcher.KindleCheckpoint(self.root / "checkpoint.sqlite3")
        checkpoint.append(1, fetcher.KindleCapture(make_sample_pdf_bytes(1), 1, None, 4, None, None))
        report = self.root / "result.json"
        args = [
            "url_pdf_fetcher", "--url", "https://read.amazon.co.jp/?asin=TEST",
            "--output", str(output), "--kindle-checkpoint", str(checkpoint.path),
            "--result-json", str(report),
        ]
        with patch.object(sys, "argv", args), patch.object(
            fetcher, "fetch_via_browser", side_effect=fetcher.CaptureCanceled
        ):
            self.assertEqual(fetcher.main(), 130)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((result["pageCount"], result["expectedPages"]), (1, 4))
        self.assertEqual(checkpoint.progress(), (1, 4))
        self.assertFalse(output.exists())

    def test_direct_pdf_download(self) -> None:
        out_pdf = self.root / "out_direct.pdf"
        pages = fetcher.fetch_and_merge(
            url=f"http://127.0.0.1:{self.port}/doc2.pdf",
            output_path=out_pdf,
            allow_browser=False,
        )
        self.assertEqual(pages, 3)
        self.assertTrue(out_pdf.exists())
        self.assertEqual(len(PdfReader(str(out_pdf)).pages), 3)

    def test_paginated_html_crawl_and_merge(self) -> None:
        out_pdf = self.root / "out_merged.pdf"
        pages = fetcher.fetch_and_merge(
            url=f"http://127.0.0.1:{self.port}/page1.html",
            output_path=out_pdf,
            allow_browser=False,
        )
        # doc1 (2) + doc2 (3) + doc3 (1) = 6 pages (doc1 duplicate skipped)
        self.assertEqual(pages, 6)
        self.assertTrue(out_pdf.exists())
        self.assertEqual(len(PdfReader(str(out_pdf)).pages), 6)

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(ValueError):
            fetcher.fetch_and_merge(
                url="ftp://example.com/file.pdf",
                output_path=self.root / "bad.pdf",
                allow_browser=False,
            )

    def test_browser_arrow_right_and_pdf_command(self) -> None:
        out_pdf = self.root / "out_viewer.pdf"
        pages = fetcher.fetch_and_merge(
            url=f"http://127.0.0.1:{self.port}/viewer.html",
            output_path=out_pdf,
            max_pages=10,
            allow_browser=True,
        )
        self.assertEqual(pages, 3)
        self.assertTrue(out_pdf.exists())
        reader = PdfReader(str(out_pdf))
        self.assertEqual(len(reader.pages), 3)
        joined_text = "\n".join(p.extract_text() or "" for p in reader.pages).replace("\x01", " ")
        self.assertIn("Slide 1", joined_text)
        self.assertIn("Slide 3", joined_text)
        self.assertNotIn("Hang tight", joined_text)

    def test_browser_arrow_left_direction(self) -> None:
        out_pdf = self.root / "out_viewer_left.pdf"
        pages = fetcher.fetch_and_merge(
            url=f"http://127.0.0.1:{self.port}/viewer_left.html",
            output_path=out_pdf,
            max_pages=10,
            allow_browser=True,
            direction="left",
        )
        self.assertEqual(pages, 2)
        reader = PdfReader(str(out_pdf))
        joined_text = "\n".join(p.extract_text() or "" for p in reader.pages).replace("\x01", " ")
        self.assertIn("RTL Page 1", joined_text)
        self.assertIn("RTL Page 2", joined_text)

    def test_long_chapter_no_clipped_lines_across_page_breaks(self) -> None:
        out_pdf = self.root / "out_long_chapter.pdf"
        pages = fetcher.fetch_and_merge(
            url=f"http://127.0.0.1:{self.port}/long_chapter.html",
            output_path=out_pdf,
            max_pages=5,
            allow_browser=True,
        )
        self.assertGreaterEqual(pages, 2)
        reader = PdfReader(str(out_pdf))
        joined_text = "\n".join(p.extract_text() or "" for p in reader.pages)
        compact_text = "".join(joined_text.split())
        for i in range(1, 65):
            self.assertIn(f"LineMarker_{i:03d}", compact_text)

    def test_fixed_layout_pf_pages_one_to_one(self) -> None:
        out_pdf = self.root / "out_fixed.pdf"
        pages = fetcher.fetch_and_merge(
            url=f"http://127.0.0.1:{self.port}/fixed_layout.html",
            output_path=out_pdf,
            max_pages=5,
            allow_browser=True,
        )
        self.assertEqual(pages, 3)
        reader = PdfReader(str(out_pdf))
        self.assertEqual(len(reader.pages), 3)

    def test_pdf_content_signature_distinguishes_image_pdfs(self) -> None:
        from PIL import Image

        def make_image_pdf(color: tuple[int, int, int]) -> bytes:
            buf = io.BytesIO()
            img = Image.new("RGB", (200, 300), color=color)
            img.save(buf, "PDF")
            return buf.getvalue()

        sig_red = fetcher.pdf_content_signature(make_image_pdf((255, 0, 0)))
        sig_blue = fetcher.pdf_content_signature(make_image_pdf((0, 0, 255)))
        self.assertNotEqual(sig_red, sig_blue)

    def test_sidebar_and_header_hidden_in_reader_pdf(self) -> None:
        out_pdf = self.root / "out_sidebar.pdf"
        pages = fetcher.fetch_and_merge(
            url=f"http://127.0.0.1:{self.port}/sidebar_layout.html",
            output_path=out_pdf,
            max_pages=2,
            allow_browser=True,
        )
        self.assertEqual(pages, 1)
        reader = PdfReader(str(out_pdf))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        self.assertIn("READER_BODY_CONTENT_LINE", text)
        self.assertNotIn("SIDEBAR_EXPLANATION_SHOULD_HIDE", text)
        self.assertNotIn("TOP_BANNER_SHOULD_HIDE", text)
        self.assertNotIn("Other formats available", text)

    def test_blank_page_raises_error_instead_of_saving_white_pdf(self) -> None:
        out_pdf = self.root / "out_blank.pdf"
        with self.assertRaises(RuntimeError):
            fetcher.fetch_and_merge(
                url=f"http://127.0.0.1:{self.port}/blank_page.html",
                output_path=out_pdf,
                max_pages=2,
                allow_browser=True,
            )
        self.assertFalse(out_pdf.exists())


if __name__ == "__main__":
    unittest.main()
