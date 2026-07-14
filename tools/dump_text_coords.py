"""Dump the per-chunk coordinates pypdf's extract_text() reports, so you can
cross-check them against the on-page positions you measure with pdf_coords.html.

The visitor signature mirrors get_frames_pdf() in config.py. cm[4]/cm[5] are the
current-transformation-matrix translation (the values you asked about); tm[4]/tm[5]
are the text-matrix translation. The text's actual user-space origin is generally
tm composed with cm, so when a glyph's measured position doesn't match cm[4]/cm[5]
on its own, compare against tm and the product too.

Usage:
    python tools/dump_text_coords.py path/to/file.pdf [--page N] [--grep WORD]
"""

import argparse
from pypdf import PdfReader


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, help="1-based page; default = all pages")
    ap.add_argument(
        "--grep", help="only rows whose text contains this (case-insensitive)"
    )
    args = ap.parse_args()

    reader = PdfReader(args.pdf)
    needle = args.grep.lower() if args.grep else None

    print(
        f"{'pg':>3} {'cm[4]':>9} {'cm[5]':>9} {'tm[4]':>9} {'tm[5]':>9} {'sz':>6}  text"
    )
    print("-" * 72)

    for i, page in enumerate(reader.pages, start=1):
        if args.page and i != args.page:
            continue

        rows: list[tuple] = []

        def visitor(text, cm, tm, _font_dict, font_size, _i=i):
            if not text.strip():
                return
            if needle and needle not in text.lower():
                return
            rows.append((_i, cm[4], cm[5], tm[4], tm[5], font_size, text))

        page.extract_text(visitor_text=visitor)

        for pg, cx, cy, tx, ty, sz, text in rows:
            shown = text.replace("\n", "\\n")
            if len(shown) > 40:
                shown = shown[:39] + "…"
            sz = sz if sz is not None else 0
            print(
                f"{pg:>3} {cx:>9.2f} {cy:>9.2f} {tx:>9.2f} {ty:>9.2f} {sz:>6.1f}  {shown}"
            )


if __name__ == "__main__":
    main()
