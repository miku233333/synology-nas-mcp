from __future__ import annotations

import os
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from synology_nas_mcp.files import FileStore
from synology_nas_mcp.index import IndexLimitError, SearchIndex


def _index(tmp_path: Path, **kwargs) -> tuple[SearchIndex, Path]:
    root = tmp_path / "share"
    root.mkdir()
    return SearchIndex(tmp_path / "search.sqlite3", FileStore(root), **kwargs), root


def _paths(results: list[dict]) -> set[str]:
    return {result["path"] for result in results}


def _write_pdf(path: Path, text: str) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page = writer.add_blank_page(width=300, height=300)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 30 200 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def _write_docx(path: Path, text: str) -> None:
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def test_indexes_utf8_pdf_docx_and_short_chinese_queries(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    nested = root / "資料"
    nested.mkdir()
    (nested / "note.txt").write_text("你好，blue moon 在 NAS。", encoding="utf-8")
    _write_pdf(nested / "report.pdf", "The blue moon is visible")
    _write_docx(nested / "draft.docx", "A blue moon appears")

    index.rebuild_or_refresh()

    assert index.status()["state"] == "complete"
    assert index.status()["fresh"] is True
    assert _paths(index.search("BLUE MOON")) == {
        "資料/note.txt",
        "資料/report.pdf",
        "資料/draft.docx",
    }
    assert _paths(index.search("你好")) == {"資料/note.txt"}
    assert _paths(index.search("你")) == {"資料/note.txt"}


@pytest.mark.parametrize("query", ["%_p", 'a"b', "[x]*"])
def test_search_treats_wildcards_and_quotes_as_literal_text(tmp_path: Path, query: str) -> None:
    index, root = _index(tmp_path)
    (root / "literal.txt").write_text('Paid 50%_paid; a"b; [x]* marker', encoding="utf-8")
    (root / "decoy.txt").write_text("Paid 50ZZpaid; axb; x marker", encoding="utf-8")

    index.rebuild_or_refresh()

    assert _paths(index.search(query)) == {"literal.txt"}


def test_content_hits_precede_filename_only_hits(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    for number in range(4):
        (root / f"needle-{number}.txt").write_text("unrelated text", encoding="utf-8")
    (root / "content.txt").write_text("the needle is here", encoding="utf-8")

    index.rebuild_or_refresh()

    results = index.search("needle", limit=2)
    assert results[0]["path"] == "content.txt"
    assert "needle" in results[0]["snippet"]
    assert results[1]["path"].startswith("needle-")


def test_refresh_updates_adds_and_removes_documents(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    changed = root / "changed.txt"
    deleted = root / "deleted.txt"
    changed.write_text("the old phrase", encoding="utf-8")
    deleted.write_text("the old phrase", encoding="utf-8")
    index.rebuild_or_refresh()
    assert _paths(index.search("old phrase")) == {"changed.txt", "deleted.txt"}

    previous_mtime_ns = changed.stat().st_mtime_ns
    changed.write_text("the new replacement phrase", encoding="utf-8")
    os.utime(changed, ns=(previous_mtime_ns + 2_000_000_000,) * 2)
    deleted.unlink()
    (root / "added.txt").write_text("the new replacement phrase", encoding="utf-8")

    index.rebuild_or_refresh()

    assert index.status()["state"] == "complete"
    assert index.search("old phrase") == []
    assert _paths(index.search("replacement phrase")) == {"changed.txt", "added.txt"}


def test_refresh_reextracts_when_text_limit_changes(tmp_path: Path) -> None:
    root = tmp_path / "share"
    root.mkdir()
    (root / "long.txt").write_text("before needle", encoding="utf-8")
    database = tmp_path / "search.sqlite3"
    SearchIndex(database, FileStore(root, max_text_chars=6)).rebuild_or_refresh()

    index = SearchIndex(database, FileStore(root, max_text_chars=50))
    index.rebuild_or_refresh()

    assert _paths(index.search("needle")) == {"long.txt"}


def test_symlinks_are_never_indexed_or_returned_after_replacement(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    outside = tmp_path / "private.txt"
    outside.write_text("private needle", encoding="utf-8")
    (root / "outside.txt").symlink_to(outside)
    visible = root / "visible.txt"
    visible.write_text("public needle", encoding="utf-8")

    index.rebuild_or_refresh()

    assert _paths(index.search("needle")) == {"visible.txt"}
    visible.unlink()
    visible.symlink_to(outside)
    assert index.search("needle") == []


def test_search_revalidates_changed_file_before_returning_hit(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    document = root / "note.txt"
    document.write_text("confidential old marker", encoding="utf-8")
    index.rebuild_or_refresh()
    assert _paths(index.search("old marker")) == {"note.txt"}

    document.write_text("different content with a different size", encoding="utf-8")

    assert index.search("old marker") == []


def test_refresh_skips_directory_removed_during_scan(tmp_path: Path, monkeypatch) -> None:
    index, root = _index(tmp_path)
    moving = root / "moving"
    moving.mkdir()
    (moving / "note.txt").write_text("moving marker", encoding="utf-8")
    (root / "stable.txt").write_text("stable marker", encoding="utf-8")
    index.rebuild_or_refresh()
    original_open = index.files._open_directory
    opens = 0

    def open_directory(parts: tuple[str, ...]) -> int:
        nonlocal opens
        if parts == ("moving",):
            opens += 1
            if opens == 2:
                (moving / "note.txt").unlink()
                moving.rmdir()
                raise FileNotFoundError(2, "directory disappeared")
        return original_open(parts)

    monkeypatch.setattr(index.files, "_open_directory", open_directory)
    index.rebuild_or_refresh()

    assert index.status()["state"] == "complete"
    assert index.status()["stats"]["errors"] == 1
    assert _paths(index.search("stable marker")) == {"stable.txt"}


def test_index_path_check_detects_same_inode_under_share(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    nested = root / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="outside the shared data directory"):
        index._ensure_outside_share(nested.stat())
    assert not index.db_path.exists()


def test_missing_data_root_makes_index_unavailable(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    document = root / "note.txt"
    document.write_text("needle", encoding="utf-8")
    index.rebuild_or_refresh()
    document.unlink()
    root.rmdir()

    assert index.status()["state"] == "unavailable"
    with pytest.raises(ValueError, match="Shared data directory"):
        index.search("needle")


def test_skips_media_oversize_and_broken_documents_with_counts(tmp_path: Path) -> None:
    root = tmp_path / "share"
    root.mkdir()
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    (root / "large.txt").write_text("needle " * 20, encoding="utf-8")
    (root / "broken.pdf").write_bytes(b"not a pdf")
    (root / "good.txt").write_text("needle", encoding="utf-8")
    index = SearchIndex(tmp_path / "search.sqlite3", FileStore(root, max_file_bytes=32))

    index.rebuild_or_refresh()

    stats = index.status()["stats"]
    assert stats["skipped_media"] >= 1
    assert stats["skipped_oversize"] >= 1
    assert stats["errors"] >= 1
    assert stats["indexed_files"] == 1
    assert _paths(index.search("needle")) == {"good.txt"}


def test_refresh_drops_previous_content_after_parse_error(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    document = root / "report.pdf"
    _write_pdf(document, "The original marker")
    (root / "healthy.txt").write_text("healthy marker", encoding="utf-8")
    index.rebuild_or_refresh()
    assert _paths(index.search("original marker")) == {"report.pdf"}

    document.write_bytes(b"invalid pdf")
    index.rebuild_or_refresh()

    assert index.status()["stats"]["errors"] >= 1
    assert _paths(index.search("healthy marker")) == {"healthy.txt"}
    with pytest.raises(ValueError):
        index.search("original marker")


def test_counts_truncated_documents(tmp_path: Path) -> None:
    root = tmp_path / "share"
    root.mkdir()
    (root / "long.txt").write_text("needle after the text limit", encoding="utf-8")
    index = SearchIndex(tmp_path / "search.sqlite3", FileStore(root, max_text_chars=6))

    index.rebuild_or_refresh()

    assert index.status()["stats"]["truncated_documents"] == 1
    assert _paths(index.search("needle")) == {"long.txt"}


def test_entry_cap_refuses_initial_index_creation_and_search(tmp_path: Path) -> None:
    index, root = _index(tmp_path, max_entries=1)
    (root / "one.txt").write_text("needle one", encoding="utf-8")
    (root / "two.txt").write_text("needle two", encoding="utf-8")

    with pytest.raises(ValueError):
        index.rebuild_or_refresh()

    assert index.status()["state"] == "unavailable"
    assert index.status()["fresh"] is False
    with pytest.raises(ValueError):
        index.search("needle")


def test_preflight_cap_invalidates_existing_index(tmp_path: Path) -> None:
    index, root = _index(tmp_path, max_entries=2)
    (root / "first.txt").write_text("needle", encoding="utf-8")
    index.rebuild_or_refresh()
    (root / "second.txt").write_text("needle", encoding="utf-8")
    (root / "third.txt").write_text("needle", encoding="utf-8")

    with pytest.raises(IndexLimitError):
        index.rebuild_or_refresh()

    assert index.status()["state"] == "partial"
    with pytest.raises(ValueError):
        index.search("needle")


def test_text_budget_marks_index_partial_and_refuses_search(tmp_path: Path) -> None:
    index, root = _index(tmp_path, max_text_bytes=4)
    (root / "note.txt").write_text("needle", encoding="utf-8")

    with pytest.raises(ValueError):
        index.rebuild_or_refresh()

    assert index.status()["state"] == "partial"
    with pytest.raises(ValueError):
        index.search("needle")


def test_database_page_cap_fails_without_committing_partial_results(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    index.rebuild_or_refresh()
    page_size = 4_096
    cap = index.db_path.stat().st_size + page_size
    (root / "large.txt").write_text("needle content " * 3_000, encoding="utf-8")
    limited = SearchIndex(index.db_path, index.files, max_db_bytes=cap)

    with pytest.raises(IndexLimitError):
        limited.rebuild_or_refresh()

    assert limited.status()["state"] == "partial"
    assert index.db_path.stat().st_size <= cap


def test_age_limit_marks_index_stale_and_refuses_search(tmp_path: Path) -> None:
    index, root = _index(tmp_path, max_age_seconds=0.001)
    (root / "note.txt").write_text("needle", encoding="utf-8")
    index.rebuild_or_refresh()
    time.sleep(0.02)

    assert index.status()["state"] == "stale"
    assert index.status()["fresh"] is False
    with pytest.raises(ValueError):
        index.search("needle")


def test_sqlite_index_can_be_opened_by_read_only_reader(tmp_path: Path) -> None:
    index, root = _index(tmp_path)
    (root / "note.txt").write_text("needle", encoding="utf-8")
    index.rebuild_or_refresh()
    database = tmp_path / "search.sqlite3"

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as reader:
        assert reader.execute("PRAGMA quick_check").fetchone() == ("ok",)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TABLE forbidden (value TEXT)")


def test_search_and_status_work_without_directory_write_access(tmp_path: Path) -> None:
    root = tmp_path / "share"
    root.mkdir()
    (root / "note.txt").write_text("needle", encoding="utf-8")
    directory = tmp_path / "private-index"
    database = directory / "search.sqlite3"
    index = SearchIndex(database, FileStore(root))
    index.rebuild_or_refresh()

    database.chmod(0o400)
    directory.chmod(0o500)
    try:
        assert index.status()["fresh"] is True
        assert _paths(index.search("needle")) == {"note.txt"}
    finally:
        directory.chmod(0o700)
        database.chmod(0o600)
