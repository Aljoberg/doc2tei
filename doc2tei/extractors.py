from __future__ import annotations

from collections.abc import Iterable, Sequence
from collections import Counter, deque
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
from typing import cast, Iterator, Literal, TYPE_CHECKING

from engine import PDFChunk, PDFPageContext, make_chunk
from type_decs import (
    CharacterGlyph,
    Chunk,
    ExtractedWord,
    LineBreakTest,
    LineEnricher,
    LineFilter,
    PageEnricher,
    PageErrorHandler,
    StopTest,
)

if TYPE_CHECKING:
    from pdfminer.layout import LTChar, LTPage


@dataclass(frozen=True)
class PageRange:
    """Zero-based, half-open page range."""

    start: int = 0
    end: int | None = None

    def contains(self, page_num: int) -> bool:
        return page_num >= self.start and (self.end is None or page_num < self.end)


@dataclass
class RunRecord:
    text: str
    x: float
    y: float
    font_name: str
    font_size: float
    space_before: bool = True


@dataclass
class LineRecord:
    text: str
    x: float
    y: float
    font_name: str
    font_size: float
    runs: list[RunRecord]
    metadata: dict[str, object] = field(default_factory=dict)
    x_end: float | None = None


@dataclass
class _CharacterRun:
    text: str
    x0: float
    y0: float
    fontname: str
    size: float
    gap: bool
    x1: float
    merge_previous: bool = False


@dataclass(frozen=True)
class _CharacterExtractionOptions:
    line_tolerance: float
    line_break_mode: Literal["absolute", "downward"]
    reverse_line_multiplier: float
    literal_spaces: Literal["preserve", "break", "ignore"]
    gap_threshold: float
    max_run_x_gap: float | None
    merge_nearby_runs: bool
    font_size_tolerance: float
    baseline_tolerance: float


@dataclass(frozen=True)
class _PDFMinerRangeTask:
    filename: str
    start: int
    end: int
    options: _CharacterExtractionOptions


@dataclass(frozen=True)
class _WordExtractionOptions:
    x_tolerance: float
    y_tolerance: float
    line_tolerance: float
    word_gap: float
    preserve_word_runs: bool


@dataclass(frozen=True)
class _PDFPlumberRangeTask:
    filename: str
    start: int
    end: int
    options: _WordExtractionOptions


@dataclass
class _PageRecordBatch:
    page_num: int
    width: float = 0.0
    height: float = 0.0
    records: list[LineRecord] = field(default_factory=list)
    pending_after: bool | None = None
    first_record_uses_initial_pending: bool = False
    error_type: str | None = None
    error_message: str | None = None


class _PageExtractionFailure(RuntimeError):
    """Serializable worker failure reconstructed in the parent process."""


def _pdfminer_page_count(filename: str) -> int:
    """Count actual page-tree leaves without interpreting page contents."""

    from pdfminer.pdfpage import PDFPage

    with open(filename, "rb") as source:
        # Do not trust a malformed PDF's declared /Pages /Count: a value that
        # is too small would silently omit trailing text from parallel ranges.
        return sum(1 for _page in PDFPage.get_pages(source))


def _extract_pdfminer_range(task: _PDFMinerRangeTask) -> list[_PageRecordBatch]:
    """Extract one contiguous page range in an isolated worker process."""

    from pdfminer.converter import PDFLayoutAnalyzer
    from pdfminer.layout import LTChar, LTPage
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage

    options = task.options
    extractor = CharacterPDFExtractor(
        line_tolerance=options.line_tolerance,
        line_break_mode=options.line_break_mode,
        reverse_line_multiplier=options.reverse_line_multiplier,
        literal_spaces=options.literal_spaces,
        gap_threshold=options.gap_threshold,
        max_run_x_gap=options.max_run_x_gap,
        merge_nearby_runs=options.merge_nearby_runs,
        font_size_tolerance=options.font_size_tolerance,
        baseline_tolerance=options.baseline_tolerance,
        page_workers=1,
    )
    resource_manager = PDFResourceManager()

    class CharCollector(PDFLayoutAnalyzer):
        def __init__(self):
            super().__init__(resource_manager, laparams=None)
            self.chars: list[LTChar] = []
            self.width = 0.0
            self.height = 0.0

        def receive_layout(self, ltpage: LTPage):
            self.width = float(ltpage.width)
            self.height = float(ltpage.height)

            def walk(obj: Iterable[object]):
                for item in obj:
                    if isinstance(item, LTChar):
                        self.chars.append(item)
                    elif isinstance(item, Iterable):
                        walk(cast(Iterable[object], item))

            walk(cast(Iterable[object], ltpage))

    device = CharCollector()
    interpreter = PDFPageInterpreter(resource_manager, device)
    batches: list[_PageRecordBatch] = []
    with open(task.filename, "rb") as source:
        for page_num, page in enumerate(PDFPage.get_pages(source)):
            if page_num < task.start:
                continue
            if page_num >= task.end:
                break
            try:
                device.chars = []
                interpreter.process_page(page)
                raw_lines = extractor._group_lines(device.chars)
                records, pending_values, uses_initial = (
                    extractor._make_records_with_state(raw_lines, False)
                )
                batches.append(
                    _PageRecordBatch(
                        page_num=page_num,
                        width=device.width,
                        height=device.height,
                        records=records,
                        pending_after=(pending_values[-1] if pending_values else None),
                        first_record_uses_initial_pending=uses_initial,
                    )
                )
            except Exception as error:
                batches.append(
                    _PageRecordBatch(
                        page_num=page_num,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                )
    return batches


def _extract_pdfplumber_range(task: _PDFPlumberRangeTask) -> list[_PageRecordBatch]:
    """Extract one pdfplumber page range in an isolated worker process."""

    import pdfplumber

    options = task.options
    extractor = WordPDFExtractor(
        x_tolerance=options.x_tolerance,
        y_tolerance=options.y_tolerance,
        line_tolerance=options.line_tolerance,
        word_gap=options.word_gap,
        preserve_word_runs=options.preserve_word_runs,
        page_workers=1,
    )
    batches: list[_PageRecordBatch] = []
    selected = list(range(task.start + 1, task.end + 1))
    with pdfplumber.open(task.filename, pages=selected) as pdf:
        for page in pdf.pages:
            page_num = page.page_number - 1
            try:
                height = float(page.height)
                words = cast(
                    list[ExtractedWord],
                    page.extract_words(
                        x_tolerance=options.x_tolerance,
                        y_tolerance=options.y_tolerance,
                        keep_blank_chars=False,
                        use_text_flow=True,
                        extra_attrs=["fontname", "size"],
                    ),
                )
                records = [
                    extractor._record(height, words_on_line)
                    for words_on_line in extractor._group_words(words)
                    if words_on_line
                ]
                batches.append(
                    _PageRecordBatch(
                        page_num=page_num,
                        width=float(page.width),
                        height=height,
                        records=[record for record in records if record.text],
                    )
                )
            except Exception as error:
                batches.append(
                    _PageRecordBatch(
                        page_num=page_num,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                )
            finally:
                # A Page retains its complete pdfminer layout and object maps
                # after extract_words(). Long documents otherwise keep every
                # page's character graph alive until the worker exits.
                page.close()
    return batches


def _parallel_main_is_safe() -> bool:
    """Whether child processes can recreate the current Python entry point."""

    if os.name != "nt":
        return True
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    return bool(
        main_file and not str(main_file).startswith("<") and Path(main_file).exists()
    )


def _page_ranges(start: int, end: int, workers: int) -> list[tuple[int, int]]:
    size, remainder = divmod(end - start, workers)
    ranges: list[tuple[int, int]] = []
    cursor = start
    for index in range(workers):
        range_size = size + (1 if index < remainder else 0)
        ranges.append((cursor, cursor + range_size))
        cursor += range_size
    return ranges


def _weighted_mode(
    words: list[ExtractedWord], key: Literal["size", "fontname"]
) -> float | str:
    counts: Counter[float | str] = Counter()
    for word in words:
        counts[word[key]] += max(1, len(str(word["text"])))
    return counts.most_common(1)[0][0]


class CharacterPDFExtractor:
    """Configurable pdfminer character pipeline.

    The class supplies collection, line/run construction and standard context;
    document configs still choose all thresholds and may replace the line-break
    policy, filtering and metadata enrichment.
    """

    def __init__(
        self,
        *,
        pages: PageRange = PageRange(),
        line_tolerance: float = 4.0,
        line_break: LineBreakTest | None = None,
        line_break_mode: Literal["absolute", "downward"] = "absolute",
        reverse_line_multiplier: float = 5.0,
        literal_spaces: Literal["preserve", "break", "ignore"] = "preserve",
        gap_threshold: float = 1.7,
        max_run_x_gap: float | None = None,
        merge_nearby_runs: bool = True,
        font_size_tolerance: float = 0.1,
        baseline_tolerance: float = 0.25,
        page_workers: int = 1,
        parallel_min_pages: int = 8,
        line_filter: LineFilter | None = None,
        stop_before: StopTest | None = None,
        page_enricher: PageEnricher | None = None,
        line_enricher: LineEnricher | None = None,
        page_error_handler: PageErrorHandler | None = None,
    ):
        self.pages = pages
        self.line_tolerance = line_tolerance
        self.line_break = line_break
        self.line_break_mode: Literal["absolute", "downward"] = line_break_mode
        self.reverse_line_multiplier = max(1.0, reverse_line_multiplier)
        self.literal_spaces: Literal["preserve", "break", "ignore"] = literal_spaces
        self.gap_threshold = gap_threshold
        self.max_run_x_gap = max_run_x_gap
        self.merge_nearby_runs = merge_nearby_runs
        self.font_size_tolerance = max(0.0, font_size_tolerance)
        self.baseline_tolerance = max(0.0, baseline_tolerance)
        self.page_workers = max(0, page_workers)
        self.parallel_min_pages = max(1, parallel_min_pages)
        self.line_filter = line_filter
        self.stop_before = stop_before
        self.page_enricher = page_enricher
        self.line_enricher = line_enricher
        self.page_error_handler = page_error_handler

    def __call__(self, filename: str) -> Iterator[Chunk]:
        batches = self._parallel_page_batches(filename)
        if batches is None:
            batches = self._sequential_page_batches(filename)
        yield from self._chunks_from_page_batches(batches)

    def _sequential_page_batches(self, filename: str) -> Iterator[_PageRecordBatch]:
        from pdfminer.converter import PDFLayoutAnalyzer
        from pdfminer.layout import LTChar
        from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
        from pdfminer.pdfpage import PDFPage

        resource_manager = PDFResourceManager()

        class CharCollector(PDFLayoutAnalyzer):
            def __init__(self):
                super().__init__(resource_manager, laparams=None)
                self.chars: list[LTChar] = []
                self.width = 0.0
                self.height = 0.0

            def receive_layout(self, ltpage: LTPage):
                self.width = float(ltpage.width)
                self.height = float(ltpage.height)

                def walk(obj: Iterable[object]):
                    for item in obj:
                        if isinstance(item, LTChar):
                            self.chars.append(item)
                        elif isinstance(item, Iterable):
                            walk(cast(Iterable[object], item))

                walk(cast(Iterable[object], ltpage))

        device = CharCollector()
        interpreter = PDFPageInterpreter(resource_manager, device)

        with open(filename, "rb") as source:
            for page_num, page in enumerate(PDFPage.get_pages(source)):
                if page_num < self.pages.start:
                    continue
                if self.pages.end is not None and page_num >= self.pages.end:
                    break

                try:
                    device.chars = []
                    interpreter.process_page(page)
                    raw_lines = self._group_lines(device.chars)
                    records, pending_values, uses_initial = (
                        self._make_records_with_state(raw_lines, False)
                    )
                except Exception as error:
                    yield _PageRecordBatch(
                        page_num=page_num,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                yield _PageRecordBatch(
                    page_num=page_num,
                    width=device.width,
                    height=device.height,
                    records=records,
                    pending_after=(pending_values[-1] if pending_values else None),
                    first_record_uses_initial_pending=uses_initial,
                )

    def _parallel_page_batches(self, filename: str) -> list[_PageRecordBatch] | None:
        """Return ordered worker results, or ``None`` for safe sequential use."""

        if self.page_workers == 1 or self.line_break is not None:
            return None
        # Windows spawn cannot safely recreate an interactive ``<stdin>`` or
        # notebook main module. CLI/module entry points have a real file.
        if not _parallel_main_is_safe():
            return None
        try:
            page_count = _pdfminer_page_count(filename)
        except Exception:
            return None

        start = min(max(0, self.pages.start), page_count)
        end = page_count if self.pages.end is None else min(self.pages.end, page_count)
        selected_pages = max(0, end - start)
        if selected_pages < self.parallel_min_pages:
            return None
        requested = (
            min(8, os.cpu_count() or 1) if self.page_workers == 0 else self.page_workers
        )
        worker_count = min(max(1, requested), selected_pages)
        if worker_count < 2:
            return None

        options = _CharacterExtractionOptions(
            line_tolerance=self.line_tolerance,
            line_break_mode=self.line_break_mode,
            reverse_line_multiplier=self.reverse_line_multiplier,
            literal_spaces=self.literal_spaces,
            gap_threshold=self.gap_threshold,
            max_run_x_gap=self.max_run_x_gap,
            merge_nearby_runs=self.merge_nearby_runs,
            font_size_tolerance=self.font_size_tolerance,
            baseline_tolerance=self.baseline_tolerance,
        )
        tasks = [
            _PDFMinerRangeTask(filename, range_start, range_end, options)
            for range_start, range_end in _page_ranges(start, end, worker_count)
        ]

        # Do not emit partial worker output. If process startup, pickling, or a
        # worker fails, the caller can transparently retry sequentially without
        # duplicating text already sent to the parser.
        try:
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                range_results = list(executor.map(_extract_pdfminer_range, tasks))
        except Exception:
            return None
        batches = [batch for result in range_results for batch in result]
        batches.sort(key=lambda batch: batch.page_num)
        if len(batches) != selected_pages:
            return None
        return batches

    def _chunks_from_page_batches(
        self, batches: Iterable[_PageRecordBatch]
    ) -> Iterator[Chunk]:
        previous_run: PDFChunk | None = None
        pending_space = False
        for batch in batches:
            page_num = batch.page_num
            if batch.error_type is not None:
                error = _PageExtractionFailure(
                    f"{batch.error_type}: {batch.error_message or ''}".rstrip()
                )
                if self.page_error_handler is None:
                    raise error
                self.page_error_handler(page_num, error)
                pending_space = True
                continue

            records = batch.records
            if batch.first_record_uses_initial_pending and records:
                records[0].runs[0].space_before = pending_space
            if batch.pending_after is not None:
                pending_space = batch.pending_after
            context = PDFPageContext(page_num, batch.width, batch.height)
            try:
                if self.page_enricher is not None:
                    self.page_enricher(context, records)
            except Exception as error:
                if self.page_error_handler is None:
                    raise
                self.page_error_handler(page_num, error)
                pending_space = True
                continue

            for index, record in enumerate(records):
                if self.stop_before is not None and self.stop_before(record, context):
                    return
                if self.line_filter is not None and not self.line_filter(
                    record, context
                ):
                    continue
                if self.line_enricher is not None:
                    record.metadata.update(
                        self.line_enricher(context, record, index, records) or {}
                    )

                flow_previous_run = previous_run
                run_chunks: list[PDFChunk] = []
                for run in record.runs:
                    previous_run = make_chunk(
                        text=run.text,
                        x=run.x,
                        y=run.y,
                        font_name=run.font_name,
                        size=run.font_size,
                        previous=previous_run,
                        page_num=page_num,
                        space_before=run.space_before,
                        page_context=context,
                    )
                    run_chunks.append(previous_run)
                if not run_chunks:
                    continue
                line_chunk = make_chunk(
                    text=record.text,
                    x=record.x,
                    y=record.y,
                    runs=run_chunks,
                    page_num=page_num,
                    page_context=context,
                    metadata=record.metadata,
                )
                for run in run_chunks:
                    run.line_chunk = line_chunk
                yield from run_chunks
                if record.metadata.get("out_of_flow"):
                    previous_run = flow_previous_run

    def _is_line_break(self, previous_y: float, current_y: float) -> bool:
        if self.line_break is not None:
            return self.line_break(previous_y, current_y)
        if self.line_break_mode == "downward":
            return (
                previous_y - current_y > self.line_tolerance
                or abs(previous_y - current_y)
                > self.line_tolerance * self.reverse_line_multiplier
            )
        return abs(previous_y - current_y) > self.line_tolerance

    def _group_lines(self, chars: list[LTChar]) -> list[list[LTChar]]:
        lines: list[list[LTChar]] = []
        current: list[LTChar] = []
        previous_y: float | None = None
        for char in chars:
            if previous_y is not None and self._is_line_break(previous_y, char.y0):
                if current:
                    lines.append(current)
                current = []
            current.append(char)
            previous_y = char.y0
        if current:
            lines.append(current)
        return lines

    def _make_records(
        self,
        lines: Sequence[Sequence[CharacterGlyph]],
        initial_pending_space: bool,
    ) -> tuple[list[LineRecord], list[bool]]:
        records, pending_values, _uses_initial = self._make_records_with_state(
            lines, initial_pending_space
        )
        return records, pending_values

    def _make_records_with_state(
        self,
        lines: Sequence[Sequence[CharacterGlyph]],
        initial_pending_space: bool,
    ) -> tuple[list[LineRecord], list[bool], bool]:
        records: list[LineRecord] = []
        pending_space = initial_pending_space
        pending_values: list[bool] = []
        initial_pending_active = True
        first_record_uses_initial = False
        pending_lines = deque(lines)
        while pending_lines:
            line = pending_lines.popleft()
            runs: list[_CharacterRun] = []
            previous: CharacterGlyph | None = None
            broke = False
            for index, char in enumerate(line):
                char_text = char.get_text()
                if char_text == " " and self.literal_spaces == "break":
                    broke = True
                    remainder = line[index + 1 :]
                    if remainder:
                        pending_lines.appendleft(remainder)
                    break
                if char_text == " " and self.literal_spaces == "ignore":
                    previous = char
                    continue
                gap = bool(previous) and char.x0 - previous.x1 > self.gap_threshold
                within_max_gap = (
                    True
                    if self.max_run_x_gap is None or previous is None
                    else abs(previous.x0 - char.x0) < self.max_run_x_gap
                )
                grouped = (
                    bool(runs)
                    and runs[-1].fontname == char.fontname
                    and runs[-1].size == char.size
                    and within_max_gap
                )
                if grouped:
                    if gap and self.literal_spaces != "preserve":
                        runs[-1].text += " "
                    runs[-1].text += char_text
                    runs[-1].x1 = float(char.x1)
                else:
                    merge_previous = bool(
                        self.merge_nearby_runs
                        # The line-start run can trigger different structural
                        # actions than later runs, so never absorb run 2 into it.
                        and len(runs) > 1
                        and runs[-1].fontname == char.fontname
                        and abs(runs[-1].size - float(char.size))
                        <= self.font_size_tolerance
                        and abs(runs[-1].y0 - float(char.y0)) <= self.baseline_tolerance
                        and within_max_gap
                    )
                    runs.append(
                        _CharacterRun(
                            text=char_text,
                            x0=float(char.x0),
                            y0=float(char.y0),
                            fontname=str(char.fontname),
                            size=float(char.size),
                            gap=bool(gap),
                            x1=float(char.x1),
                            merge_previous=merge_previous,
                        )
                    )
                previous = char

            if not runs or not "".join(run.text for run in runs).strip():
                pending_space = not broke
                pending_values.append(pending_space)
                if not records:
                    initial_pending_active = False
                continue

            run_records: list[RunRecord] = []
            for index, run in enumerate(runs):
                if index == 0:
                    space_before = pending_space
                elif self.literal_spaces == "preserve":
                    prior = runs[index - 1].text
                    current = run.text
                    space_before = prior[-1:].isspace() or current[:1].isspace()
                else:
                    space_before = run.gap
                run_records.append(
                    RunRecord(
                        text=run.text,
                        x=run.x0,
                        y=run.y0,
                        font_name=run.fontname,
                        font_size=run.size,
                        space_before=space_before,
                    )
                )
            text = "".join(run.text for run in run_records)
            if self.merge_nearby_runs:
                run_records = self._merge_run_records(run_records, runs)
            first = run_records[0]
            if not records:
                first_record_uses_initial = initial_pending_active
            records.append(
                LineRecord(
                    text=text,
                    x=first.x,
                    y=first.y,
                    font_name=first.font_name,
                    font_size=first.font_size,
                    runs=run_records,
                    x_end=max(run.x1 for run in runs),
                )
            )
            pending_space = not broke if self.literal_spaces == "break" else True
            pending_values.append(pending_space)
        return records, pending_values, first_record_uses_initial

    def _merge_run_records(
        self,
        records: list[RunRecord],
        source_runs: list[_CharacterRun],
    ) -> list[RunRecord]:
        """Coalesce size-jitter runs while preserving old rendered spacing.

        ``engine.append`` strips each original run and inserts at most one
        leading space from ``space_before``. Rebuilding merged text with those
        same rules prevents literal-space glyphs from introducing new or lost
        whitespace. The first physical run is deliberately kept separate
        because line-start rules may treat it specially.
        """

        if len(records) < 3 or not any(run.merge_previous for run in source_runs[1:]):
            return records

        groups: list[list[RunRecord]] = [[records[0]]]
        protected = [False]
        for index, (record, source) in enumerate(
            zip(records[1:], source_runs[1:]), start=1
        ):
            group = groups[-1]
            anchor = group[0]
            if (
                source.merge_previous
                and len(groups) > 1
                and not protected[-1]
                and anchor.font_name == record.font_name
                and abs(anchor.font_size - record.font_size) <= self.font_size_tolerance
                and abs(anchor.y - record.y) <= self.baseline_tolerance
            ):
                group.append(record)
            else:
                groups.append([record])
                # The first run after a font/style transition must remain a
                # separate append operation. The cosmetic stack closes the old
                # style on that run and opens the new style on the next one.
                protected.append(source.fontname != source_runs[index - 1].fontname)

        merged: list[RunRecord] = []
        for group in groups:
            if len(group) == 1:
                merged.append(group[0])
                continue
            visible = [(run, run.text.strip()) for run in group if run.text.strip()]
            if not visible:
                merged.append(group[0])
                continue
            parts: list[str] = []
            for run, text in visible:
                if parts and run.space_before:
                    parts.append(" ")
                parts.append(text)
            first, _text = visible[0]
            merged.append(
                RunRecord(
                    text="".join(parts),
                    x=first.x,
                    y=first.y,
                    font_name=first.font_name,
                    font_size=first.font_size,
                    space_before=first.space_before,
                )
            )
        return merged


class WordPDFExtractor:
    """Configurable pdfplumber word-to-line pipeline."""

    def __init__(
        self,
        *,
        pages: PageRange = PageRange(),
        x_tolerance: float = 1.7,
        y_tolerance: float = 3.0,
        line_tolerance: float = 3.2,
        word_gap: float = 0.6,
        join_line_end_hyphens: bool = False,
        preserve_word_runs: bool = False,
        page_workers: int = 1,
        parallel_min_pages: int = 8,
        line_filter: LineFilter | None = None,
        stop_before: StopTest | None = None,
        page_enricher: PageEnricher | None = None,
        line_enricher: LineEnricher | None = None,
        page_error_handler: PageErrorHandler | None = None,
    ):
        self.pages = pages
        self.x_tolerance = x_tolerance
        self.y_tolerance = y_tolerance
        self.line_tolerance = line_tolerance
        self.word_gap = word_gap
        self.join_line_end_hyphens = join_line_end_hyphens
        self.preserve_word_runs = preserve_word_runs
        self.page_workers = max(0, page_workers)
        self.parallel_min_pages = max(1, parallel_min_pages)
        self.line_filter = line_filter
        self.stop_before = stop_before
        self.page_enricher = page_enricher
        self.line_enricher = line_enricher
        self.page_error_handler = page_error_handler

    def __call__(self, filename: str) -> Iterator[Chunk]:
        parallel_batches = self._parallel_page_batches(filename)
        if parallel_batches is not None:
            yield from self._chunks_from_parallel_batches(parallel_batches)
            return

        import pdfplumber

        previous_run: PDFChunk | None = None
        pending_space = False
        with pdfplumber.open(filename) as pdf:
            for page_num, page in enumerate(pdf.pages):
                if page_num < self.pages.start:
                    continue
                if self.pages.end is not None and page_num >= self.pages.end:
                    break
                try:
                    context = PDFPageContext(
                        page_num=page_num,
                        width=float(page.width),
                        height=float(page.height),
                    )
                    words = cast(
                        list[ExtractedWord],
                        page.extract_words(
                            x_tolerance=self.x_tolerance,
                            y_tolerance=self.y_tolerance,
                            keep_blank_chars=False,
                            use_text_flow=True,
                            extra_attrs=["fontname", "size"],
                        ),
                    )
                    records = [
                        self._record(context.height, words_on_line)
                        for words_on_line in self._group_words(words)
                        if words_on_line
                    ]
                    records = [record for record in records if record.text]
                    if self.page_enricher is not None:
                        self.page_enricher(context, records)
                except Exception as error:
                    if self.page_error_handler is None:
                        raise
                    self.page_error_handler(page_num, error)
                    pending_space = True
                    continue
                finally:
                    # Records contain all data needed below; release the much
                    # larger pdfminer page layout before moving to the next page.
                    page.close()

                for index, record in enumerate(records):
                    if self.stop_before is not None and self.stop_before(
                        record, context
                    ):
                        return
                    if self.line_filter is not None and not self.line_filter(
                        record, context
                    ):
                        continue
                    if self.line_enricher is not None:
                        record.metadata.update(
                            self.line_enricher(context, record, index, records) or {}
                        )
                    flow_previous_run = previous_run
                    flow_pending_space = pending_space
                    text = record.text
                    hyphenated = (
                        not record.metadata.get("out_of_flow")
                        and self.join_line_end_hyphens
                        and text.endswith(("-", "‐", "‑"))
                    )
                    if hyphenated:
                        text = text[:-1]
                    source_runs = (
                        record.runs
                        if self.preserve_word_runs
                        else [
                            RunRecord(
                                text=text,
                                x=record.x,
                                y=record.y,
                                font_name=record.font_name,
                                font_size=record.font_size,
                                space_before=pending_space,
                            )
                        ]
                    )
                    if self.preserve_word_runs and hyphenated and source_runs:
                        source_runs[-1].text = source_runs[-1].text[:-1]
                    run_chunks: list[PDFChunk] = []
                    for run_index, run in enumerate(source_runs):
                        previous_run = make_chunk(
                            text=run.text,
                            x=run.x,
                            y=run.y,
                            font_name=run.font_name,
                            size=run.font_size,
                            previous=previous_run,
                            page_num=page_num,
                            space_before=(
                                pending_space if run_index == 0 else run.space_before
                            ),
                            page_context=context,
                        )
                        run_chunks.append(previous_run)
                    if not run_chunks:
                        continue
                    line_chunk = make_chunk(
                        text=text,
                        x=record.x,
                        y=record.y,
                        runs=run_chunks,
                        page_num=page_num,
                        page_context=context,
                        metadata=record.metadata,
                    )
                    for run in run_chunks:
                        run.line_chunk = line_chunk
                    yield from run_chunks
                    pending_space = not hyphenated
                    if record.metadata.get("out_of_flow"):
                        previous_run = flow_previous_run
                        pending_space = flow_pending_space

    def _parallel_page_batches(self, filename: str) -> list[_PageRecordBatch] | None:
        if self.page_workers == 1 or not _parallel_main_is_safe():
            return None
        try:
            page_count = _pdfminer_page_count(filename)
        except Exception:
            return None

        start = min(max(0, self.pages.start), page_count)
        end = page_count if self.pages.end is None else min(self.pages.end, page_count)
        selected_pages = max(0, end - start)
        if selected_pages < self.parallel_min_pages:
            return None
        requested = (
            min(8, os.cpu_count() or 1) if self.page_workers == 0 else self.page_workers
        )
        worker_count = min(max(1, requested), selected_pages)
        if worker_count < 2:
            return None

        options = _WordExtractionOptions(
            x_tolerance=self.x_tolerance,
            y_tolerance=self.y_tolerance,
            line_tolerance=self.line_tolerance,
            word_gap=self.word_gap,
            preserve_word_runs=self.preserve_word_runs,
        )
        tasks = [
            _PDFPlumberRangeTask(filename, range_start, range_end, options)
            for range_start, range_end in _page_ranges(start, end, worker_count)
        ]
        try:
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                range_results = list(executor.map(_extract_pdfplumber_range, tasks))
        except Exception:
            return None
        batches = [batch for result in range_results for batch in result]
        batches.sort(key=lambda batch: batch.page_num)
        if len(batches) != selected_pages:
            return None
        return batches

    def _chunks_from_parallel_batches(
        self, batches: Iterable[_PageRecordBatch]
    ) -> Iterator[Chunk]:
        previous_run: PDFChunk | None = None
        pending_space = False
        for batch in batches:
            page_num = batch.page_num
            if batch.error_type is not None:
                error = _PageExtractionFailure(
                    f"{batch.error_type}: {batch.error_message or ''}".rstrip()
                )
                if self.page_error_handler is None:
                    raise error
                self.page_error_handler(page_num, error)
                pending_space = True
                continue

            records = batch.records
            context = PDFPageContext(page_num, batch.width, batch.height)
            try:
                if self.page_enricher is not None:
                    self.page_enricher(context, records)
            except Exception as error:
                if self.page_error_handler is None:
                    raise
                self.page_error_handler(page_num, error)
                pending_space = True
                continue

            for index, record in enumerate(records):
                if self.stop_before is not None and self.stop_before(record, context):
                    return
                if self.line_filter is not None and not self.line_filter(
                    record, context
                ):
                    continue
                if self.line_enricher is not None:
                    record.metadata.update(
                        self.line_enricher(context, record, index, records) or {}
                    )
                flow_previous_run = previous_run
                flow_pending_space = pending_space
                text = record.text
                hyphenated = (
                    not record.metadata.get("out_of_flow")
                    and self.join_line_end_hyphens
                    and text.endswith(("-", "‐", "‑"))
                )
                if hyphenated:
                    text = text[:-1]
                source_runs = (
                    record.runs
                    if self.preserve_word_runs
                    else [
                        RunRecord(
                            text=text,
                            x=record.x,
                            y=record.y,
                            font_name=record.font_name,
                            font_size=record.font_size,
                            space_before=pending_space,
                        )
                    ]
                )
                if self.preserve_word_runs and hyphenated and source_runs:
                    source_runs[-1].text = source_runs[-1].text[:-1]
                run_chunks: list[PDFChunk] = []
                for run_index, run in enumerate(source_runs):
                    previous_run = make_chunk(
                        text=run.text,
                        x=run.x,
                        y=run.y,
                        font_name=run.font_name,
                        size=run.font_size,
                        previous=previous_run,
                        page_num=page_num,
                        space_before=(
                            pending_space if run_index == 0 else run.space_before
                        ),
                        page_context=context,
                    )
                    run_chunks.append(previous_run)
                if not run_chunks:
                    continue
                line_chunk = make_chunk(
                    text=text,
                    x=record.x,
                    y=record.y,
                    runs=run_chunks,
                    page_num=page_num,
                    page_context=context,
                    metadata=record.metadata,
                )
                for run in run_chunks:
                    run.line_chunk = line_chunk
                yield from run_chunks
                pending_space = not hyphenated
                if record.metadata.get("out_of_flow"):
                    previous_run = flow_previous_run
                    pending_space = flow_pending_space

    def _group_words(self, words: list[ExtractedWord]) -> list[list[ExtractedWord]]:
        lines: list[list[ExtractedWord]] = []
        current: list[ExtractedWord] = []
        anchor_top: float | None = None
        for word in words:
            top = float(word["top"])
            if (
                current
                and anchor_top is not None
                and abs(top - anchor_top) > self.line_tolerance
            ):
                lines.append(sorted(current, key=lambda item: float(item["x0"])))
                current = []
                anchor_top = None
            current.append(word)
            if anchor_top is None or float(word["size"]) >= 9:
                anchor_top = top
        if current:
            lines.append(sorted(current, key=lambda item: float(item["x0"])))
        return lines

    def _record(self, page_height: float, words: list[ExtractedWord]) -> LineRecord:
        parts: list[str] = []
        runs: list[RunRecord] = []
        previous = None
        for word in words:
            spaced = (
                previous is not None
                and float(word["x0"]) - float(previous["x1"]) > self.word_gap
            )
            if spaced:
                parts.append(" ")
            word_text = str(word["text"])
            parts.append(word_text)
            word_size = float(word["size"])
            word_font = str(word["fontname"])
            word_y = page_height - float(word["bottom"])
            if (
                self.preserve_word_runs
                and runs
                and runs[-1].font_name == word_font
                and abs(runs[-1].font_size - word_size) < 0.05
                and abs(runs[-1].y - word_y) < 0.75
            ):
                if spaced:
                    runs[-1].text += " "
                runs[-1].text += word_text
            elif self.preserve_word_runs:
                runs.append(
                    RunRecord(
                        word_text,
                        float(word["x0"]),
                        word_y,
                        word_font,
                        word_size,
                        space_before=spaced,
                    )
                )
            previous = word
        size = float(_weighted_mode(words, "size"))
        font = str(_weighted_mode(words, "fontname"))
        baseline = next(
            (word for word in words if abs(float(word["size"]) - size) < 0.05),
            words[0],
        )
        text = "".join(parts).strip()
        x = min(float(word["x0"]) for word in words)
        y = page_height - float(baseline["bottom"])
        if not self.preserve_word_runs:
            runs = [RunRecord(text, x, y, font, size)]
        return LineRecord(
            text,
            x,
            y,
            font,
            size,
            runs,
            x_end=max(float(word["x1"]) for word in words),
        )
