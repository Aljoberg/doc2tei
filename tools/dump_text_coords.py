"""Inspect the text coordinates that doc2tei's pdfminer backend sees.

Coordinates use PDF space: ``x0``/``y0`` is the lower-left corner and
``x1``/``y1`` is the upper-right corner. Use ``--level char`` when a text line
contains mixed fonts, sizes, or raised footnote markers.

Usage:
    python tools/dump_text_coords.py file.pdf [--page N] [--grep WORD]
    python tools/dump_text_coords.py file.pdf --page 3 --level char
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar, LTContainer, LTPage, LTTextLine

Level = Literal["line", "char"]


@dataclass(frozen=True)
class CoordinateRow:
    page: int
    kind: Level
    x0: float
    y0: float
    x1: float
    y1: float
    size: float | None
    font: str
    text: str


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return number


def _walk_layout(item: object) -> Iterator[object]:
    yield item
    if isinstance(item, LTContainer):
        for child in cast(Iterable[object], item):
            yield from _walk_layout(child)


def _line_style(line: LTTextLine) -> tuple[float | None, str]:
    characters = [item for item in _walk_layout(line) if isinstance(item, LTChar)]
    if not characters:
        return None, ""
    sizes = {round(float(character.size), 3) for character in characters}
    fonts = {str(character.fontname) for character in characters}
    size = sizes.pop() if len(sizes) == 1 else None
    font = fonts.pop() if len(fonts) == 1 else "<mixed>"
    return size, font


def _page_rows(page_number: int, page: LTPage, level: Level) -> Iterator[CoordinateRow]:
    for item in _walk_layout(page):
        if level == "line" and isinstance(item, LTTextLine):
            size, font = _line_style(item)
            yield CoordinateRow(
                page=page_number,
                kind="line",
                x0=float(item.x0),
                y0=float(item.y0),
                x1=float(item.x1),
                y1=float(item.y1),
                size=size,
                font=font,
                text=item.get_text().rstrip("\r\n"),
            )
        elif level == "char" and isinstance(item, LTChar):
            yield CoordinateRow(
                page=page_number,
                kind="char",
                x0=float(item.x0),
                y0=float(item.y0),
                x1=float(item.x1),
                y1=float(item.y1),
                size=float(item.size),
                font=str(item.fontname),
                text=item.get_text(),
            )


def coordinate_rows(
    pdf: Path,
    *,
    page_number: int | None = None,
    level: Level = "line",
) -> Iterator[CoordinateRow]:
    page_numbers = [page_number - 1] if page_number is not None else None
    for index, page in enumerate(
        extract_pages(str(pdf), page_numbers=page_numbers),
        start=page_number or 1,
    ):
        yield from _page_rows(index, page, level)


def _visible_text(text: str, width: int) -> str:
    visible = (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return visible if len(visible) <= width else f"{visible[: width - 3]}..."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dump pdfminer text bounding boxes, fonts, and sizes."
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=positive_integer, help="1-based page number")
    parser.add_argument(
        "--grep",
        help="only rows whose text contains this value (case-insensitive)",
    )
    parser.add_argument(
        "--level",
        choices=("line", "char"),
        default="line",
        help="emit reconstructed text lines or individual characters (default: line)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    pdf: Path = args.pdf.expanduser().resolve()
    if not pdf.is_file():
        parser.error(f"PDF does not exist: {pdf}")
    if pdf.suffix.casefold() != ".pdf":
        parser.error(f"input is not a PDF: {pdf}")

    needle = args.grep.casefold() if args.grep else None
    print(
        f"{'pg':>4} {'kind':<4} {'x0':>9} {'y0':>9} {'x1':>9} {'y1':>9} "
        f"{'size':>7} {'font':<24} text"
    )
    print("-" * 112)
    try:
        rows = coordinate_rows(
            pdf,
            page_number=args.page,
            level=args.level,
        )
        for row in rows:
            if needle is not None and needle not in row.text.casefold():
                continue
            size = f"{row.size:.2f}" if row.size is not None else "mixed"
            print(
                f"{row.page:>4} {row.kind:<4} "
                f"{row.x0:>9.2f} {row.y0:>9.2f} "
                f"{row.x1:>9.2f} {row.y1:>9.2f} "
                f"{size:>7} {row.font[:24]:<24} {_visible_text(row.text, 80)}"
            )
    except Exception as error:
        parser.exit(
            1, f"error: could not inspect {pdf}: {type(error).__name__}: {error}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
