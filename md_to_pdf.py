#!/usr/bin/env python3
"""Convert a Markdown draft to a clean PDF for Telegram attachments.

Two stages, each on the interpreter that has the needed lib:
  1. md -> HTML  : system python3 (has the 'markdown' lib)
  2. HTML -> PDF : the canvas-check venv python via Playwright's page.pdf()
                   (the same headless-Chrome stack auto_login.py already uses;
                    raw `chrome --print-to-pdf` hangs over SSH, Playwright doesn't).

Run with SYSTEM python3.  Usage: md_to_pdf.py <in.md> <out.pdf>  (exit 0 on success)

The rendering interpreter is $RENDER_PYTHON if set, else the canvas-check venv
python under $CANVAS_CHECK_HOME if present, else this interpreter.
"""
import os
import sys
import subprocess
import tempfile
import html as _html
from pathlib import Path

_venv_py = Path(os.environ.get("CANVAS_CHECK_HOME", Path.home() / "canvas-check")) / ".venv" / "bin" / "python"
VENV_PY = os.environ.get("RENDER_PYTHON") or (str(_venv_py) if _venv_py.exists() else sys.executable)

CSS = """
@page { margin: 0.9in; }
body { font-family: Georgia, 'Times New Roman', serif; font-size: 12pt;
       line-height: 1.55; color: #111; }
h1, h2, h3 { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 0.6em 0 0.3em; }
h1 { font-size: 18pt; } h2 { font-size: 15pt; } h3 { font-size: 13pt; }
p { margin: 0 0 0.7em; }
ul, ol { margin: 0 0 0.7em 1.2em; }
code, pre { font-family: 'SF Mono', Menlo, monospace; font-size: 10.5pt; }
pre { background: #f4f4f4; padding: 8px; border-radius: 4px; white-space: pre-wrap; }
"""

# Rendered by the venv python (has Playwright). Args: <html-file> <out-pdf>.
RENDER = r"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
html_file, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True,
                          args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_page()
    pg.goto(Path(html_file).as_uri(), wait_until="load")
    pg.pdf(path=out, format="Letter", print_background=True,
           margin={"top": "0.8in", "bottom": "0.8in", "left": "0.9in", "right": "0.9in"})
    b.close()
print("PDF_OK")
"""


def to_html(md_text, title):
    try:
        import markdown
        body = markdown.markdown(md_text, extensions=["extra", "sane_lists", "nl2br"])
    except Exception:
        body = "".join(f"<p>{_html.escape(p)}</p>" for p in md_text.split("\n\n"))
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{_html.escape(title)}</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")


def main():
    if len(sys.argv) < 3:
        print("usage: md_to_pdf.py <in.md> <out.pdf>", file=sys.stderr)
        return 2
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    if out.exists():
        out.unlink()
    html_doc = to_html(src.read_text(encoding="utf-8", errors="replace"), src.stem)
    with tempfile.TemporaryDirectory() as td:
        htmlf = Path(td) / "draft.html"
        htmlf.write_text(html_doc, encoding="utf-8")
        renderf = Path(td) / "render.py"
        renderf.write_text(RENDER)
        try:
            r = subprocess.run([VENV_PY, str(renderf), str(htmlf), str(out)],
                               capture_output=True, text=True, timeout=120)
        except Exception as e:
            print(f"FAIL render: {e}", file=sys.stderr)
            return 1
    if out.exists() and out.stat().st_size > 0:
        print(f"OK {out} ({out.stat().st_size} bytes)")
        return 0
    print(f"FAIL no pdf: {(r.stdout or '')[-200:]} {(r.stderr or '')[-400:]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
