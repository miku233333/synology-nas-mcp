from __future__ import annotations

import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, RectangleObject

import synology_nas_mcp.files as files_module
from synology_nas_mcp.files import FileStore


def _write_pdf(path: Path, texts: list[str]) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 30 200 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def _write_compressed_content_pdf(path: Path, content: bytes) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    with path.open("wb") as output:
        writer.write(output)


def _compressed_stream(writer: PdfWriter, content: bytes, *, form: bool = False):
    stream = DecodedStreamObject()
    stream.set_data(content)
    if form:
        stream[NameObject("/Type")] = NameObject("/XObject")
        stream[NameObject("/Subtype")] = NameObject("/Form")
        stream[NameObject("/BBox")] = RectangleObject((0, 0, 10, 10))
        stream[NameObject("/Resources")] = DictionaryObject()
    return writer._add_object(stream.flate_encode())


def _write_resource_pdf(path: Path, form_contents: list[bytes], *, shared: bool) -> None:
    writer = PdfWriter()
    shared_form = _compressed_stream(writer, form_contents[0], form=True) if shared else None
    page_count = 2 if shared else 1
    for _page_index in range(page_count):
        page = writer.add_blank_page(width=300, height=300)
        if shared_form is not None:
            forms = {NameObject("/Fm1"): shared_form}
            invocation = b"q /Fm1 Do Q"
        else:
            forms = {
                NameObject(f"/Fm{index}"): _compressed_stream(writer, content, form=True)
                for index, content in enumerate(form_contents, start=1)
            }
            invocation = b" ".join(
                f"/Fm{index} Do".encode("ascii") for index in range(1, len(forms) + 1)
            )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject(forms)}
        )
        content_stream = DecodedStreamObject()
        content_stream.set_data(invocation)
        page[NameObject("/Contents")] = writer._add_object(content_stream)
    with path.open("wb") as output:
        writer.write(output)


def test_lists_and_reads_unicode_relative_files(tmp_path: Path) -> None:
    folder = tmp_path / "資料"
    folder.mkdir()
    (folder / "筆記.txt").write_text("你好，Synology 👋", encoding="utf-8")

    store = FileStore(tmp_path)

    listing = store.list_directory("資料")
    assert listing["entries"] == [
        {
            "name": "筆記.txt",
            "path": "資料/筆記.txt",
            "type": "file",
            "size": len("你好，Synology 👋".encode()),
        }
    ]
    assert store.read_file("資料/筆記.txt") == {
        "path": "資料/筆記.txt",
        "content": "你好，Synology 👋",
        "size": len("你好，Synology 👋".encode()),
        "media_type": "text/plain; charset=utf-8",
        "truncated": False,
    }


@pytest.mark.parametrize("path", ["../secret.txt", "folder/../../secret.txt", "/etc/passwd"])
def test_rejects_traversal_and_absolute_paths(tmp_path: Path, path: str) -> None:
    store = FileStore(tmp_path)

    with pytest.raises(ValueError):
        store.read_file(path)


def test_symlink_cannot_escape_root_and_is_not_listed_or_searched(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "secret-link.txt"
    link.symlink_to(outside)
    directory_link = root / "outside-dir"
    directory_link.symlink_to(tmp_path, target_is_directory=True)
    store = FileStore(root)

    with pytest.raises(OSError):
        store.read_file("secret-link.txt")
    with pytest.raises(OSError):
        store.read_file("outside-dir/outside.txt")
    assert store.list_directory()["entries"] == []
    assert store.search_files("outside")["results"] == []


def test_enforces_byte_limit_and_truncates_text_limit(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_bytes(b"12345")
    (tmp_path / "long.txt").write_text("abcdef", encoding="utf-8")

    with pytest.raises(OSError):
        FileStore(tmp_path, max_file_bytes=4).read_file("large.txt")

    result = FileStore(tmp_path, max_text_chars=3).read_file("long.txt")
    assert result["content"] == "abc"
    assert result["truncated"] is True


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with pytest.raises(OSError, match="regular file"):
        FileStore(tmp_path).read_file("pipe")


def test_filename_search_is_recursive_case_insensitive_and_truncated(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    for name in ("Report-One.txt", "report-two.txt", "REPORT-three.txt"):
        (nested / name).write_text(name, encoding="utf-8")
    (nested / "unrelated.txt").write_text("report is only in content", encoding="utf-8")

    result = FileStore(tmp_path).search_files("report", limit=2)

    assert len(result["results"]) == 2
    assert all("report" in item["name"].casefold() for item in result["results"])
    assert result["truncated"] is True
    assert result["scanned_entries"] == 5


def test_content_search_reads_nested_utf8_pdf_and_docx(tmp_path: Path) -> None:
    nested = tmp_path / "資料"
    nested.mkdir()
    (nested / "note.txt").write_text("The NAS contains a blue moon.", encoding="utf-8")
    _write_pdf(nested / "report.pdf", ["Blue Moon appears in this report."])
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>Another blue moon.</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(nested / "draft.docx", "w") as archive:
        archive.writestr("word/document.xml", xml)
    (nested / "only-name-blue-moon.txt").write_text("No matching content", encoding="utf-8")

    result = FileStore(tmp_path).search_content("BLUE MOON", path="資料")

    assert {item["path"] for item in result["results"]} == {
        "資料/note.txt",
        "資料/report.pdf",
        "資料/draft.docx",
    }
    assert all("blue moon" in item["snippet"].casefold() for item in result["results"])
    assert result["scanned_files"] == 4
    assert result["skipped_files"] == 0
    assert result["truncated"] is False


def test_content_search_excludes_symlinks_and_skips_unreadable_files(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private needle", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "valid.txt").write_text("visible needle", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\xff\xfe")
    (root / "link.txt").symlink_to(outside)

    result = FileStore(root).search_content("needle")

    assert [item["path"] for item in result["results"]] == ["valid.txt"]
    assert result["skipped_files"] == 1
    assert result["scanned_files"] == 2
    assert result["truncated"] is True
    with pytest.raises(ValueError):
        FileStore(root).search_content("needle", path="../")


def test_content_search_skips_media_before_document_budget(tmp_path: Path) -> None:
    for index in range(70):
        (tmp_path / f"image-{index}.jpg").write_bytes(b"\xff\xd8\xff")
    (tmp_path / "note.txt").write_text("the needle is here", encoding="utf-8")

    result = FileStore(tmp_path).search_content("needle")

    assert [item["path"] for item in result["results"]] == ["note.txt"]
    assert result["scanned_files"] == 1
    assert result["skipped_files"] == 70
    assert result["truncated"] is False


def test_content_search_marks_oversize_file_as_skipped(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("many bytes", encoding="utf-8")
    (tmp_path / "small.txt").write_text("hit", encoding="utf-8")

    result = FileStore(tmp_path, max_file_bytes=4).search_content("hit")

    assert [item["path"] for item in result["results"]] == ["small.txt"]
    assert result["skipped_files"] == 1
    assert result["truncated"] is True


def test_content_search_snippet_uses_original_unicode_offset(tmp_path: Path) -> None:
    (tmp_path / "unicode.txt").write_text("ß" * 100 + "needle", encoding="utf-8")

    result = FileStore(tmp_path).search_content("needle")

    assert "needle" in result["results"][0]["snippet"]


def test_content_search_accounts_for_actual_size_after_file_change(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(files_module, "_MAX_CONTENT_SEARCH_BYTES", 10)
    (tmp_path / "first.txt").write_text("x", encoding="utf-8")
    (tmp_path / "second.txt").write_text("x", encoding="utf-8")
    store = FileStore(tmp_path)

    def changed_file(path: str) -> dict:
        return {"path": path, "content": "needle", "size": 11, "truncated": False}

    monkeypatch.setattr(store, "read_file", changed_file)
    result = store.search_content("needle")

    assert result["scanned_files"] == 1
    assert result["scanned_bytes"] == 11
    assert result["skipped_files"] == 1
    assert result["results"] == []
    assert result["truncated"] is True


def test_content_search_honors_file_and_byte_budgets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(files_module, "_MAX_CONTENT_SEARCH_FILES", 1)
    (tmp_path / "first.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "second.txt").write_text("needle", encoding="utf-8")

    result = FileStore(tmp_path).search_content("needle")

    assert result["scanned_files"] == 1
    assert result["truncated"] is True
    assert len(result["results"]) == 1

    monkeypatch.setattr(files_module, "_MAX_CONTENT_SEARCH_FILES", 64)
    monkeypatch.setattr(files_module, "_MAX_CONTENT_SEARCH_BYTES", 6)
    result = FileStore(tmp_path).search_content("needle")
    assert result["scanned_bytes"] == 6
    assert result["truncated"] is True


def test_content_search_skips_oversize_candidate_and_continues(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(files_module, "_MAX_CONTENT_SEARCH_BYTES", 6)
    (tmp_path / "large.txt").write_text("1234567", encoding="utf-8")
    (tmp_path / "small.txt").write_text("match", encoding="utf-8")

    result = FileStore(tmp_path).search_content("match")

    assert [item["path"] for item in result["results"]] == ["small.txt"]
    assert result["skipped_files"] == 1
    assert result["truncated"] is True


def test_content_search_reports_text_and_entry_truncation(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("before needle after", encoding="utf-8")
    clipped = FileStore(tmp_path, max_text_chars=6).search_content("needle")
    assert clipped["results"] == []
    assert clipped["truncated"] is True

    capped = FileStore(tmp_path, max_search_entries=1).search_content("before")
    assert capped["scanned_entries"] == 1
    assert capped["truncated"] is False


def test_content_search_rejects_empty_and_oversized_queries(tmp_path: Path) -> None:
    store = FileStore(tmp_path)
    for query in ("", "  ", "a" * 201, "a\x00b"):
        with pytest.raises(ValueError):
            store.search_content(query)


def test_search_scan_cap_is_configurable(tmp_path: Path) -> None:
    for name in ("one.txt", "two.txt", "three.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")

    result = FileStore(tmp_path, max_search_entries=1).search_files("txt")

    assert result["scanned_entries"] == 1
    assert result["truncated"] is True


def test_path_depth_is_bounded_and_deeper_search_is_truncated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(files_module, "_MAX_RELATIVE_DEPTH", 2)
    deep = tmp_path / "one" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "target.txt").write_text("target", encoding="utf-8")
    store = FileStore(tmp_path)

    result = store.search_files("target")

    assert result["results"] == []
    assert result["truncated"] is True
    listing = store.list_directory("one/two")
    assert listing["entries"] == []
    assert listing["truncated"] is True
    with pytest.raises(ValueError, match="maximum directory depth"):
        store.read_file("one/two/three/target.txt")


def test_reads_pdf_text_and_stops_at_character_limit(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    _write_pdf(pdf, ["Hello PDF"])

    complete = FileStore(tmp_path).read_file("sample.pdf")
    truncated = FileStore(tmp_path, max_text_chars=5).read_file("sample.pdf")

    assert complete["content"] == "Hello PDF"
    assert complete["media_type"] == "application/pdf"
    assert complete["truncated"] is False
    assert truncated["content"] == "Hello"
    assert truncated["truncated"] is True


def test_rejects_invalid_pdf(tmp_path: Path) -> None:
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")

    with pytest.raises(ValueError, match="readable PDF"):
        FileStore(tmp_path).read_file("broken.pdf")


def test_pdf_page_count_is_bounded(tmp_path: Path) -> None:
    pdf = tmp_path / "many-pages.pdf"
    _write_pdf(pdf, [""] * 201)

    result = FileStore(tmp_path).read_file("many-pages.pdf")

    assert result["truncated"] is True


def test_docx_archive_entry_count_is_bounded(tmp_path: Path) -> None:
    document = tmp_path / "too-many-entries.docx"
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr("word/document.xml", "<document />")
        for index in range(1_024):
            archive.writestr(f"padding/{index}", b"")

    with pytest.raises(ValueError, match="too many archive entries"):
        FileStore(tmp_path).read_file(document.name)


def test_reads_docx_text_and_stops_at_character_limit(tmp_path: Path) -> None:
    document = tmp_path / "sample.docx"
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>abcdef</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr("word/document.xml", xml)

    result = FileStore(tmp_path, max_text_chars=3).read_file(document.name)

    assert result["content"] == "abc"
    assert result["truncated"] is True


def test_rejects_small_docx_with_oversized_expanded_xml(tmp_path: Path) -> None:
    document = tmp_path / "compressed-bomb.docx"
    oversized_xml = b"<document>" + b" " * (4 * 1024 * 1024) + b"</document>"
    with zipfile.ZipFile(document, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", oversized_xml)
    assert document.stat().st_size < 100_000

    with pytest.raises(ValueError, match="safety limit"):
        FileStore(tmp_path).read_file(document.name)


def test_rejects_small_pdf_with_oversized_decoded_stream(tmp_path: Path) -> None:
    document = tmp_path / "compressed-bomb.pdf"
    _write_compressed_content_pdf(document, b" " * (4 * 1024 * 1024 + 1))
    assert document.stat().st_size < 100_000

    with pytest.raises(ValueError, match="readable PDF"):
        FileStore(tmp_path).read_file(document.name)


@pytest.mark.parametrize("fake_image", [False, True])
def test_pdf_decoded_budget_accumulates_across_pages(tmp_path: Path, fake_image: bool) -> None:
    document = tmp_path / "two-large-pages.pdf"
    writer = PdfWriter()
    for _index in range(2):
        page = writer.add_blank_page(width=300, height=300)
        stream = _compressed_stream(writer, b" " * (3 * 1024 * 1024))
        if fake_image:
            stream.get_object()[NameObject("/Subtype")] = NameObject("/Image")
        page[NameObject("/Contents")] = stream
    with document.open("wb") as output:
        writer.write(output)
    assert document.stat().st_size < 100_000

    with pytest.raises(ValueError, match="readable PDF"):
        FileStore(tmp_path).read_file(document.name)


def test_pdf_decoded_budget_accumulates_form_xobjects(tmp_path: Path) -> None:
    document = tmp_path / "two-large-forms.pdf"
    _write_resource_pdf(
        document,
        [b" " * (3 * 1024 * 1024), b" " * (3 * 1024 * 1024)],
        shared=False,
    )
    assert document.stat().st_size < 100_000

    with pytest.raises(ValueError, match="readable PDF"):
        FileStore(tmp_path).read_file(document.name)


def test_pdf_shared_resource_is_counted_once(tmp_path: Path) -> None:
    document = tmp_path / "shared-form.pdf"
    _write_resource_pdf(document, [b" " * (3 * 1024 * 1024)], shared=True)

    result = FileStore(tmp_path).read_file(document.name)

    assert result["media_type"] == "application/pdf"
    assert result["truncated"] is False


def test_document_parsing_is_serialized_per_store(tmp_path: Path, monkeypatch) -> None:
    for name in ("one.docx", "two.docx"):
        (tmp_path / name).write_bytes(b"placeholder")
    store = FileStore(tmp_path)
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def slow_read(_file_object) -> tuple[str, bool]:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return "done", False

    monkeypatch.setattr(store, "_read_docx", slow_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(store.read_file, ("one.docx", "two.docx")))

    assert [result["content"] for result in results] == ["done", "done"]
    assert maximum_active == 1


def test_root_open_error_does_not_expose_absolute_path(tmp_path: Path) -> None:
    missing_root = tmp_path / "private-root"

    with pytest.raises(OSError) as raised:
        FileStore(missing_root).list_directory()

    assert str(missing_root) not in str(raised.value)
