from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import streamlit as st

from doc2tei.batch import DEFAULT_CORPUS_PREFIX, default_metadata_root
from doc2tei.web import (
    build_corpus_archive,
    command_display,
    launch_pipeline,
    log_tail,
    manifest_artifacts,
    manifest_counts,
    manifest_items,
    parse_lines,
    read_manifest,
    stop_pipeline,
    validate_pipeline_request,
)
from type_decs import ListPersonScope, PipelineRequest, PipelineRun, UploadedDocument

PROJECT_ROOT = Path(__file__).parent.resolve()
DEFAULT_CONFIG = PROJECT_ROOT / "examples" / "general-config" / "config.py"
DEFAULT_OUTPUT = PROJECT_ROOT / "out" / "web-corpus"
SOURCE_OPTIONS = ("Local paths", "Upload files", "SIstory")

st.set_page_config(
    page_title="doc2tei pipeline",
    page_icon=":material/account_tree:",
    layout="wide",
)

st.session_state.setdefault("pipeline_run", None)
st.session_state.setdefault("pipeline_errors", [])
st.session_state.setdefault("pipeline_finished_run", None)

run = cast(PipelineRun | None, st.session_state.get("pipeline_run"))
is_running = run is not None and run.process.poll() is None

with st.sidebar:
    st.subheader("doc2tei")
    st.caption("Local TEI conversion control panel")
    st.markdown(
        "- General, recovery-oriented parsing\n"
        "- Recursive TEI corpus output\n"
        "- Separate human-review audit tree"
    )
    st.caption(f"Project: `{PROJECT_ROOT}`")

st.title("Build a TEI corpus")
st.write(
    "Select documents or a SIstory menu, configure the batch, and monitor the "
    "same recovery-oriented pipeline used by `batch_parse.py`."
)

source_values = st.pills(
    "Input sources",
    SOURCE_OPTIONS,
    default=["Local paths"],
    selection_mode="multi",
    key="source_modes",
    persist_state="session",
    disabled=is_running,
)
source_modes = set(source_values) if isinstance(source_values, list) else set()

with st.form("pipeline_form", border=True):
    st.subheader("Sources", anchor=False)
    local_paths_text = ""
    uploaded_files: list[object] = []
    sistory_text = ""

    if "Local paths" in source_modes:
        local_paths_text = st.text_area(
            "Local files or directories",
            placeholder="D:\\documents\nD:\\more-documents\\minutes.pdf",
            help="Enter one PDF, DOCX, or directory per line.",
            key="local_paths",
            disabled=is_running,
        )
    if "Upload files" in source_modes:
        uploaded_value = st.file_uploader(
            "PDF or DOCX files",
            type=("pdf", "docx"),
            accept_multiple_files=True,
            max_upload_size=1024,
            key="uploaded_documents",
            disabled=is_running,
        )
        if isinstance(uploaded_value, list):
            uploaded_files = list(uploaded_value)
        st.caption(
            "Uploads are retained below the audit directory. For very large "
            "collections, use a local directory path instead."
        )
    if "SIstory" in source_modes:
        sistory_text = st.text_area(
            "SIstory menu paths",
            placeholder="1/7/397/407",
            help="Enter one bare menu path or complete SIstory menu URL per line.",
            key="sistory_menus",
            disabled=is_running,
        )

    st.subheader("Destinations", anchor=False)
    destination_columns = st.columns(2)
    with destination_columns[0]:
        output_text = st.text_input(
            "Output container directory",
            value=str(DEFAULT_OUTPUT),
            key="output_root",
            disabled=is_running,
        )
    with destination_columns[1]:
        metadata_text = st.text_input(
            "Audit directory",
            placeholder="Leave empty for a sibling <output>-metadata directory",
            key="metadata_root",
            disabled=is_running,
        )
    config_text = st.text_input(
        "Config file",
        value=str(DEFAULT_CONFIG),
        key="config_path",
        disabled=is_running,
    )

    st.subheader("Corpus options", anchor=False)
    option_columns = st.columns(3)
    with option_columns[0]:
        emit_corpus = st.checkbox(
            "Emit recursive corpus XML",
            value=True,
            disabled=is_running,
        )
        include_root_corpus = st.checkbox(
            "Include aggregate root corpus",
            value=False,
            help=(
                "Forge an additional aggregate corpus XML covering every "
                "document in all top-level corpora."
            ),
            disabled=is_running,
        )
        write_list_person = st.checkbox(
            "Generate listPerson and listOrg",
            value=True,
            disabled=is_running,
        )
    with option_columns[1]:
        pretty = st.checkbox("Pretty-print XML", value=True, disabled=is_running)
        xml_declaration = st.checkbox(
            "Include XML declarations",
            value=True,
            disabled=is_running,
        )
    with option_columns[2]:
        recursive = st.checkbox(
            "Discover directories recursively",
            value=True,
            disabled=is_running,
        )
        overwrite = st.checkbox(
            "Reprocess unchanged documents",
            value=False,
            disabled=is_running,
        )

    list_scope_value = st.segmented_control(
        "Person-list scope when corpus XML is disabled",
        options=("document", "folder", "corpus"),
        default="document",
        required=True,
        disabled=is_running,
    )
    list_scope = cast(ListPersonScope, list_scope_value)
    st.caption(
        "Recursive corpus generation creates flat person and organisation lists "
        "for every corpus level, so this scope is used only when corpus XML is off."
    )

    with st.expander("Advanced options", icon=":material/tune:"):
        performance_columns = st.columns(3)
        with performance_columns[0]:
            workers = int(
                st.number_input(
                    "Document workers",
                    min_value=0,
                    max_value=64,
                    value=0,
                    help="0 automatically chooses up to four workers.",
                    disabled=is_running,
                )
            )
        with performance_columns[1]:
            page_workers_value = st.selectbox(
                "Page workers per document",
                ("Config default", "1", "2", "4", "8"),
                disabled=is_running,
            )
            page_workers = (
                None
                if page_workers_value == "Config default"
                else int(page_workers_value)
            )
        with performance_columns[2]:
            reuse_workers = st.checkbox(
                "Reuse document workers",
                value=False,
                help="Faster for small files; recycling is safer for memory-heavy PDFs.",
                disabled=is_running,
            )

        identity_columns = st.columns(3)
        with identity_columns[0]:
            corpus_prefix = st.text_input(
                "Corpus prefix",
                value=DEFAULT_CORPUS_PREFIX,
                disabled=is_running,
            )
        with identity_columns[1]:
            corpus_code = st.text_input(
                "Corpus code",
                value="SI",
                disabled=is_running,
            )
        with identity_columns[2]:
            corpus_language = st.text_input(
                "Corpus language",
                value="sl",
                disabled=is_running,
            )
        include_wikidata = st.checkbox(
            "Include Wikidata enrichment",
            value=False,
            disabled=is_running,
        )
        sistory_download_text = st.text_input(
            "Custom SIstory download cache",
            placeholder="Optional; defaults below the audit directory",
            disabled=is_running,
        )

    submitted = st.form_submit_button(
        "Start pipeline",
        type="primary",
        icon=":material/play_arrow:",
        disabled=is_running,
        width="stretch",
    )

if submitted:
    output_root = Path(output_text).expanduser().resolve()
    metadata_root = (
        Path(metadata_text).expanduser().resolve()
        if metadata_text.strip()
        else default_metadata_root(output_root)
    )
    uploads = tuple(
        UploadedDocument(
            name=str(getattr(upload, "name", "document.pdf")),
            data=bytes(getattr(upload, "getvalue")()),
        )
        for upload in uploaded_files
    )
    request = PipelineRequest(
        output_root=output_root,
        metadata_root=metadata_root,
        config=Path(config_text).expanduser().resolve(),
        local_inputs=tuple(
            Path(value).expanduser().resolve()
            for value in parse_lines(local_paths_text)
        ),
        uploads=uploads,
        sistory_menus=parse_lines(sistory_text),
        sistory_download_root=(
            Path(sistory_download_text).expanduser().resolve()
            if sistory_download_text.strip()
            else None
        ),
        workers=workers,
        page_workers=page_workers,
        recursive=recursive,
        write_list_person=write_list_person,
        list_person_scope=list_scope,
        emit_corpus=emit_corpus,
        include_root_corpus=include_root_corpus,
        corpus_language=corpus_language.strip(),
        corpus_prefix=corpus_prefix.strip(),
        corpus_code=corpus_code.strip(),
        include_wikidata=include_wikidata,
        pretty=pretty,
        xml_declaration=xml_declaration,
        overwrite=overwrite,
        reuse_workers=reuse_workers,
    )
    errors = validate_pipeline_request(request)
    st.session_state["pipeline_errors"] = errors
    if not errors:
        try:
            st.session_state["pipeline_run"] = launch_pipeline(request, PROJECT_ROOT)
        except Exception as error:
            st.session_state["pipeline_errors"] = [
                f"Could not start the pipeline: {type(error).__name__}: {error}"
            ]
        else:
            st.session_state["pipeline_finished_run"] = None
            st.rerun()

for error_message in cast(list[str], st.session_state.get("pipeline_errors", [])):
    st.error(error_message, icon=":material/error:")


@st.fragment(run_every=1.0 if is_running else None)
def pipeline_monitor() -> None:
    current = cast(PipelineRun | None, st.session_state.get("pipeline_run"))
    if current is None:
        st.info(
            "Configure the run above. Unchanged completed documents are skipped "
            "automatically on later runs.",
            icon=":material/info:",
        )
        return

    return_code = current.process.poll()
    running = return_code is None
    if not running and st.session_state.get("pipeline_finished_run") != current.run_id:
        st.session_state["pipeline_finished_run"] = current.run_id
        st.rerun()
    manifest = read_manifest(current)
    counts = manifest_counts(manifest)
    items = manifest_items(manifest)
    total_value = manifest.get("document_count") if manifest is not None else None
    total = total_value if isinstance(total_value, int) else 0
    completed = sum(counts.values())
    manifest_status = (
        str(manifest.get("status", "starting")) if manifest is not None else "starting"
    )
    warning_value = manifest.get("warning_count", 0) if manifest is not None else 0
    warning_count = warning_value if isinstance(warning_value, int) else 0

    with st.container(horizontal=True):
        st.metric("Documents", f"{completed}/{total or '?'}", border=True)
        st.metric("Successful", counts.get("ok", 0), border=True)
        st.metric("Skipped", counts.get("skipped", 0), border=True)
        st.metric(
            "Needs review",
            counts.get("recovered", 0) + counts.get("failed", 0) + warning_count,
            border=True,
        )

    if total:
        st.progress(
            min(completed / total, 1.0),
            text=f"Processed {completed} of {total} documents",
        )
    elif running:
        st.progress(0, text="Acquiring inputs and discovering documents")

    if running:
        status_label = f"Pipeline running · {manifest_status}"
        status_state = "running"
    elif return_code == 0:
        status_label = "Pipeline complete"
        status_state = "complete"
    else:
        status_label = f"Pipeline stopped with exit code {return_code}"
        status_state = "error"

    status = st.status(
        status_label,
        state=status_state,
        expanded=running,
    )
    tail = log_tail(current.log_path)
    status.code(tail or "Waiting for pipeline output…", language=None, height=240)

    with st.container(horizontal=True):
        if running and st.button(
            "Stop pipeline",
            icon=":material/stop_circle:",
            key=f"stop-{current.run_id}",
        ):
            stop_pipeline(current)
            st.rerun()
        if not running and st.button(
            "Start another run",
            icon=":material/replay:",
            key=f"reset-{current.run_id}",
        ):
            st.session_state["pipeline_run"] = None
            st.session_state["pipeline_errors"] = []
            st.session_state["pipeline_finished_run"] = None
            st.rerun()

    if items:
        rows = []
        for item in items:
            source = item.get("source")
            rows.append(
                {
                    "Document": Path(source).name if isinstance(source, str) else "",
                    "Status": item.get("status", ""),
                    "Seconds": item.get("elapsed_seconds", 0),
                    "Warnings": item.get("warning_count", 0),
                    "Recoveries": item.get("recovery_count", 0),
                    "Message": item.get("message", ""),
                }
            )
        st.subheader("Documents", anchor=False)
        st.dataframe(
            rows,
            hide_index=True,
            column_config={
                "Seconds": st.column_config.NumberColumn(format="%.1f"),
            },
            key=f"items-{current.run_id}",
        )

    if not running:
        st.subheader("Outputs", anchor=False)
        st.code(str(current.output_root), language=None)
        artifacts = manifest_artifacts(manifest)
        if artifacts:
            archive_name = f"{current.output_root.name or 'tei-corpus'}.zip"
            st.download_button(
                "Download output ZIP",
                data=lambda: build_corpus_archive(
                    current.output_root,
                    tuple(artifacts),
                ),
                file_name=archive_name,
                mime="application/zip",
                icon=":material/folder_zip:",
                type="primary",
                on_click="ignore",
                key=f"archive-{current.run_id}",
            )
            st.caption(
                f"Contains {len(artifacts)} generated XML file(s) with their "
                "corpus folders. Audit metadata and source downloads are excluded."
            )
        with st.container(horizontal=True, vertical_alignment="bottom"):
            if manifest is not None:
                manifest_text = (
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                )
                st.download_button(
                    "Download manifest",
                    data=manifest_text,
                    file_name="batch-manifest.json",
                    mime="application/json",
                    icon=":material/download:",
                    on_click="ignore",
                    key=f"manifest-{current.run_id}",
                )
            if artifacts:
                selected = st.selectbox(
                    "XML artifact",
                    artifacts,
                    format_func=lambda path: path.name,
                    key=f"artifact-{current.run_id}",
                )
                st.download_button(
                    "Download selected XML",
                    data=lambda: selected.read_bytes(),
                    file_name=selected.name,
                    mime="application/xml",
                    icon=":material/download:",
                    on_click="ignore",
                    key=f"download-{current.run_id}",
                )

    with st.expander("Run details", icon=":material/terminal:"):
        st.caption(f"Audit directory: `{current.metadata_root}`")
        st.caption(f"Log: `{current.log_path}`")
        st.code(
            command_display(current.command), language="powershell", wrap_lines=True
        )


pipeline_monitor()
