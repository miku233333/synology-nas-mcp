"""Read-only, root-confined file access for Synology NAS shares."""

from __future__ import annotations

import errno
import io
import os
import stat
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from xml.etree import ElementTree

_MAX_SEARCH_ENTRIES = 10_000
_MAX_LIMIT = 1_000
_MAX_RELATIVE_DEPTH = 64
_MAX_DOCX_ENTRIES = 1_024
_MAX_DOCX_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_DOCX_XML_BYTES = 4 * 1024 * 1024
_MAX_DOCX_XML_ELEMENTS = 100_000
_MAX_DOCX_XML_DEPTH = 256
_MAX_PDF_PAGES = 200
_MAX_PDF_DECODED_BYTES = 4 * 1024 * 1024
_MAX_PDF_GRAPH_DEPTH = 32
_MAX_PDF_GRAPH_OBJECTS = 10_000


class _PDFTextLimitReached(Exception):
    """Stop PDF extraction after the configured character budget is filled."""


class FileStore:
    """Expose bounded read-only operations below a single filesystem root."""

    def __init__(
        self,
        root: Path,
        max_file_bytes: int = 2_097_152,
        max_text_chars: int = 50_000,
        max_search_entries: int = _MAX_SEARCH_ENTRIES,
    ) -> None:
        if max_file_bytes <= 0 or max_text_chars <= 0 or max_search_entries <= 0:
            raise ValueError("File and search limits must be positive")
        self._root = Path(os.path.abspath(os.fspath(root)))
        self.max_file_bytes = max_file_bytes
        self.max_text_chars = max_text_chars
        self.max_search_entries = max_search_entries
        self._document_parse_slots = threading.BoundedSemaphore(value=1)

    def list_directory(self, path: str = ".", limit: int = 100, offset: int = 0) -> dict:
        """List regular files and directories without following symbolic links."""
        self._validate_limit(limit)
        if offset < 0:
            raise ValueError("Offset must not be negative")
        parts = self._path_parts(path)
        directory_fd = self._open_directory(parts)
        entries: list[dict] = []
        scan_truncated = False
        try:
            with os.scandir(directory_fd) as iterator:
                for index, entry in enumerate(iterator):
                    if index >= self.max_search_entries:
                        scan_truncated = True
                        break
                    if len(parts) + 1 > _MAX_RELATIVE_DEPTH:
                        scan_truncated = True
                        continue
                    item = self._directory_item(entry, parts)
                    if item is not None:
                        entries.append(item)
        finally:
            os.close(directory_fd)

        entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        page = entries[offset : offset + limit]
        return {
            "path": self._display_path(parts),
            "entries": page,
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "truncated": scan_truncated or offset + limit < len(entries),
        }

    def search_files(self, query: str, path: str = ".", limit: int = 100) -> dict:
        """Recursively find regular files whose names contain the query."""
        if not isinstance(query, str) or not query:
            raise ValueError("Search query must not be empty")
        if "\x00" in query:
            raise ValueError("Search query contains a NUL byte")
        self._validate_limit(limit)
        base_parts = self._path_parts(path)
        # Validate the starting directory before beginning the bounded walk.
        start_fd = self._open_directory(base_parts)
        os.close(start_fd)

        needle = query.casefold()
        pending = [base_parts]
        results: list[dict] = []
        scanned = 0
        more_matches = False
        scan_truncated = False

        while pending:
            directory_parts = pending.pop()
            directory_fd = self._open_directory(directory_parts)
            child_directories: list[tuple[str, ...]] = []
            exhausted = True
            try:
                with os.scandir(directory_fd) as iterator:
                    for entry in iterator:
                        if scanned >= self.max_search_entries:
                            exhausted = False
                            scan_truncated = True
                            break
                        scanned += 1
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        mode = metadata.st_mode
                        child_parts = directory_parts + (entry.name,)
                        if len(child_parts) > _MAX_RELATIVE_DEPTH:
                            scan_truncated = True
                            continue
                        if stat.S_ISDIR(mode):
                            # Opening later with O_NOFOLLOW revalidates the entry.
                            child_directories.append(child_parts)
                        elif stat.S_ISREG(mode) and needle in entry.name.casefold():
                            item = {
                                "name": entry.name,
                                "path": self._display_path(child_parts),
                                "size": metadata.st_size,
                            }
                            if len(results) < limit:
                                results.append(item)
                            else:
                                more_matches = True
            finally:
                os.close(directory_fd)

            if not exhausted:
                break
            child_directories.sort(key=lambda item: (item[-1].casefold(), item[-1]), reverse=True)
            pending.extend(child_directories)

        if pending:
            scan_truncated = True
        results.sort(key=lambda item: (item["path"].casefold(), item["path"]))
        return {
            "query": query,
            "path": self._display_path(base_parts),
            "results": results,
            "returned": len(results),
            "scanned_entries": scanned,
            "truncated": more_matches or scan_truncated,
        }

    def read_file(self, path: str) -> dict:
        """Read bounded UTF-8, PDF, or DOCX text from a regular file."""
        parts = self._path_parts(path)
        if not parts:
            raise ValueError("A file path is required")
        file_fd, _ = self._open_regular_file(parts)
        suffix = Path(parts[-1]).suffix.casefold()
        try:
            with os.fdopen(file_fd, "rb", closefd=True) as file_object:
                data = file_object.read(self.max_file_bytes + 1)
                if len(data) > self.max_file_bytes:
                    raise OSError(errno.EFBIG, "File exceeds the configured size limit")
            file_object = io.BytesIO(data)
            extraction_truncated = False
            try:
                if suffix == ".docx":
                    with self._document_parse_slots:
                        content, extraction_truncated = self._read_docx(file_object)
                    media_type = (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    )
                elif suffix == ".pdf":
                    with self._document_parse_slots:
                        content, extraction_truncated = self._read_pdf(file_object)
                    media_type = "application/pdf"
                else:
                    content = self._read_utf8(file_object)
                    media_type = "text/plain; charset=utf-8"
            finally:
                file_object.close()
        except (ValueError, OSError):
            raise
        except Exception as error:
            raise ValueError("The file could not be read") from error

        truncated = extraction_truncated or len(content) > self.max_text_chars
        if len(content) > self.max_text_chars:
            content = content[: self.max_text_chars]
        return {
            "path": self._display_path(parts),
            "content": content,
            "size": len(data),
            "media_type": media_type,
            "truncated": truncated,
        }

    @staticmethod
    def _path_parts(path: str) -> tuple[str, ...]:
        if not isinstance(path, str):
            raise ValueError("Path must be a string")
        if "\x00" in path:
            raise ValueError("Path contains a NUL byte")
        if PurePosixPath(path).is_absolute() or os.path.isabs(path):
            raise ValueError("Absolute paths are not allowed")
        raw_parts = path.split("/")
        if any(part == ".." for part in raw_parts):
            raise ValueError("Parent directory traversal is not allowed")
        parts = tuple(part for part in raw_parts if part not in ("", "."))
        if len(parts) > _MAX_RELATIVE_DEPTH:
            raise ValueError("Path exceeds the maximum directory depth")
        return parts

    @staticmethod
    def _display_path(parts: tuple[str, ...]) -> str:
        return "/".join(parts) if parts else "."

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("Limit must be an integer")
        if limit <= 0 or limit > _MAX_LIMIT:
            raise ValueError(f"Limit must be between 1 and {_MAX_LIMIT}")

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            return os.open(self._root, flags)
        except OSError as error:
            raise OSError(error.errno, error.strerror) from None

    def _open_directory(self, parts: tuple[str, ...]) -> int:
        current_fd = self._open_root()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            for part in parts:
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def _open_regular_file(self, parts: tuple[str, ...]) -> tuple[int, int]:
        parent_fd = self._open_directory(parts[:-1])
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            file_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(errno.EINVAL, "Path is not a regular file")
            if metadata.st_size > self.max_file_bytes:
                raise OSError(errno.EFBIG, "File exceeds the configured size limit")
            return file_fd, metadata.st_size
        except Exception:
            os.close(file_fd)
            raise

    def _directory_item(self, entry: os.DirEntry, parent_parts: tuple[str, ...]) -> dict | None:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(metadata.st_mode):
            item_type = "directory"
            size = None
        elif stat.S_ISREG(metadata.st_mode):
            item_type = "file"
            size = metadata.st_size
        else:
            return None
        return {
            "name": entry.name,
            "path": self._display_path(parent_parts + (entry.name,)),
            "type": item_type,
            "size": size,
        }

    def _read_utf8(self, file_object: BinaryIO) -> str:
        data = file_object.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes:
            raise OSError(errno.EFBIG, "File exceeds the configured size limit")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("File is not valid UTF-8 text") from error

    def _read_docx(self, file_object: BinaryIO) -> tuple[str, bool]:
        try:
            with zipfile.ZipFile(file_object) as archive:
                total_size = 0
                document_info = None
                entries = archive.infolist()
                if len(entries) > _MAX_DOCX_ENTRIES:
                    raise ValueError("DOCX contains too many archive entries")
                for info in entries:
                    total_size += info.file_size
                    if total_size > _MAX_DOCX_TOTAL_BYTES:
                        raise ValueError("DOCX expanded content exceeds the safety limit")
                    if info.flag_bits & 0x1:
                        raise ValueError("Encrypted DOCX files are not supported")
                    if info.filename == "word/document.xml":
                        document_info = info
                if document_info is None:
                    raise ValueError("DOCX document content is missing")
                if document_info.file_size > _MAX_DOCX_XML_BYTES:
                    raise ValueError("DOCX document content exceeds the safety limit")
                with archive.open(document_info) as document:
                    xml_data = document.read(_MAX_DOCX_XML_BYTES + 1)
                if len(xml_data) > _MAX_DOCX_XML_BYTES:
                    raise ValueError("DOCX document content exceeds the safety limit")
        except zipfile.BadZipFile as error:
            raise ValueError("File is not a valid DOCX document") from error

        if b"<!DOCTYPE" in xml_data.upper() or b"<!ENTITY" in xml_data.upper():
            raise ValueError("DOCX document XML contains unsupported declarations")
        fragments: list[str] = []
        character_count = 0
        element_count = 0
        element_depth = 0
        budget = self.max_text_chars + 1
        truncated = False
        try:
            for event, element in ElementTree.iterparse(
                io.BytesIO(xml_data), events=("start", "end")
            ):
                if event == "start":
                    element_count += 1
                    element_depth += 1
                    if element_count > _MAX_DOCX_XML_ELEMENTS:
                        raise ValueError("DOCX document XML contains too many elements")
                    if element_depth > _MAX_DOCX_XML_DEPTH:
                        raise ValueError("DOCX document XML nesting is too deep")
                    continue
                local_name = element.tag.rsplit("}", 1)[-1]
                text = ""
                if local_name == "t" and element.text:
                    text = element.text
                elif local_name == "tab":
                    text = "\t"
                elif local_name == "br":
                    text = "\n"
                elif local_name == "p" and fragments and fragments[-1] != "\n":
                    text = "\n"
                if text:
                    remaining = budget - character_count
                    fragments.append(text[:remaining])
                    character_count += min(len(text), remaining)
                    if character_count >= budget:
                        truncated = True
                        element.clear()
                        break
                element.clear()
                element_depth -= 1
        except ElementTree.ParseError as error:
            raise ValueError("DOCX document XML is invalid") from error
        return "".join(fragments).rstrip("\n"), truncated

    def _read_pdf(self, file_object: BinaryIO) -> tuple[str, bool]:
        try:
            from pypdf import PdfReader, apply_configuration
        except ImportError as error:
            raise ValueError("PDF support requires the optional 'pypdf' package") from error

        try:
            with apply_configuration(
                maximum_declared_stream_length=_MAX_PDF_DECODED_BYTES,
                array_based_stream_maximum_output_length=_MAX_PDF_DECODED_BYTES,
                jbig2_maximum_output_length=_MAX_PDF_DECODED_BYTES,
                lzw_maximum_output_length=_MAX_PDF_DECODED_BYTES,
                run_length_maximum_output_length=_MAX_PDF_DECODED_BYTES,
                zlib_maximum_output_length=_MAX_PDF_DECODED_BYTES,
                zlib_maximum_recovery_input_length=1 * 1024 * 1024,
                image_maximum_buffer_size=_MAX_PDF_DECODED_BYTES,
                xmp_maximum_input_length=1 * 1024 * 1024,
                xmp_maximum_element_count=20_000,
                page_tree_maximum_entries=1_000,
                xform_maximum_invocations_per_extraction=32,
                disable_legacy_handling=True,
            ):
                reader = PdfReader(file_object, strict=False)
                pages = []
                page_limit_truncated = False
                for page_index, page in enumerate(reader.pages):
                    if page_index >= _MAX_PDF_PAGES:
                        page_limit_truncated = True
                        break
                    pages.append(page)
                self._validate_pdf_decoded_budget(pages)
                fragments: list[str] = []
                character_count = 0
                budget = self.max_text_chars + 1
                truncated = page_limit_truncated
                for page in pages:
                    page_started = False

                    def collect_text(text: str, *_args: object) -> None:
                        nonlocal character_count, page_started, truncated
                        if not text:
                            return
                        if not page_started:
                            page_started = True
                            if fragments:
                                fragments.append("\n")
                                character_count += 1
                        remaining = budget - character_count
                        if remaining <= 0:
                            truncated = True
                            raise _PDFTextLimitReached
                        fragments.append(text[:remaining])
                        character_count += min(len(text), remaining)
                        if len(text) > remaining or character_count >= budget:
                            truncated = True
                            raise _PDFTextLimitReached

                    try:
                        page.extract_text(visitor_text=collect_text)
                    except _PDFTextLimitReached:
                        break
                return "".join(fragments), truncated
        except Exception as error:
            raise ValueError("File is not a readable PDF document") from error

    @staticmethod
    def _validate_pdf_decoded_budget(pages: list[object]) -> None:
        """Bound all decoded streams reachable from page contents and resources."""
        from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

        pending: list[tuple[object, int]] = []
        for page in pages:
            for key in ("/Contents", "/Resources"):
                if key in page:
                    pending.append((page.raw_get(key), 0))

        seen_indirect: set[tuple[int, int, int]] = set()
        seen_direct: set[int] = set()
        decoded_streams: set[tuple[str, int, int, int] | tuple[str, int]] = set()
        traversed_objects = 0
        decoded_bytes = 0

        while pending:
            node, depth = pending.pop()
            if depth > _MAX_PDF_GRAPH_DEPTH:
                raise ValueError("PDF resource graph exceeds the safety depth")

            indirect_key = None
            if isinstance(node, IndirectObject):
                indirect_key = (id(node.pdf), node.idnum, node.generation)
                if indirect_key in seen_indirect:
                    continue
                seen_indirect.add(indirect_key)
                traversed_objects += 1
                node = node.get_object()
            elif isinstance(node, (ArrayObject, DictionaryObject)):
                direct_key = id(node)
                if direct_key in seen_direct:
                    continue
                seen_direct.add(direct_key)
                traversed_objects += 1
            else:
                continue

            if traversed_objects > _MAX_PDF_GRAPH_OBJECTS:
                raise ValueError("PDF resource graph contains too many objects")

            if isinstance(node, StreamObject):
                stream_key: tuple[str, int, int, int] | tuple[str, int]
                if indirect_key is not None:
                    stream_key = ("indirect", *indirect_key)
                else:
                    stream_key = ("direct", id(node))
                if stream_key not in decoded_streams:
                    decoded_streams.add(stream_key)
                    decoded_length = len(node.get_data())
                    if decoded_length >= _MAX_PDF_DECODED_BYTES:
                        raise ValueError("PDF stream content exceeds the safety limit")
                    decoded_bytes += decoded_length
                    if decoded_bytes > _MAX_PDF_DECODED_BYTES:
                        raise ValueError("PDF decoded content exceeds the safety limit")

            if isinstance(node, DictionaryObject):
                for key in node.keys():
                    pending.append((node.raw_get(key), depth + 1))
            elif isinstance(node, ArrayObject):
                pending.extend((value, depth + 1) for value in node)
