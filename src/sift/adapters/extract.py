"""Turn a file into Evidence. Reads never follow symlinks; size and time are capped."""
from __future__ import annotations

import mimetypes
import os
import re
import resource
import shutil
import subprocess
import zipfile
from pathlib import Path

from ..core.model import Evidence, FileRef
from .secrets import looks_sensitive

OFFICE_ZIP = {".docx": ["word/document.xml"], ".xlsx": ["xl/sharedStrings.xml"], ".pptx": [f"ppt/slides/slide{i}.xml" for i in (1, 2, 3)],
              ".odt": ["content.xml"], ".ods": ["content.xml"], ".odp": ["content.xml"]}
_TAGS = re.compile(r"<[^>]+>")

HEAD_BYTES = 32 * 1024
MAX_FILE_FOR_EXTRACT = 50 * 1024 * 1024
TEXT_EXT = {".txt", ".md", ".rst", ".log", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf",
            ".py", ".js", ".ts", ".c", ".h", ".cpp", ".hpp", ".cc", ".rs", ".go", ".java", ".sh", ".qml",
            ".html", ".xml", ".sql", ".lua", ".eml", ".tex", ".patch", ".diff", ".gcode"}


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))


def stat_ref(path: Path) -> FileRef:
    st = os.stat(path, follow_symlinks=False)
    return FileRef(str(path), st.st_ino, st.st_mtime_ns, st.st_size)


def _read_head(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return os.read(fd, HEAD_BYTES).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _office_text(path: Path, members: list[str]) -> str | None:
    """Text from an OOXML/ODF zip. Reads at most 256 KB per member (zip-bomb guard). The file is opened without following symlinks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh, zipfile.ZipFile(fh) as z:
            names = set(z.namelist())
            chunks = []
            for m in members:
                if m in names:
                    with z.open(m) as f:
                        chunks.append(_TAGS.sub(" ", f.read(256 * 1024).decode("utf-8", errors="replace")))
        return re.sub(r"\s+", " ", " ".join(chunks)).strip()[:HEAD_BYTES] or None
    except (zipfile.BadZipFile, OSError, KeyError):
        return None


class FileExtractor:
    def extract(self, path: Path) -> Evidence:
        ref = stat_ref(path)
        mime = mimetypes.guess_type(path.name)[0] or ""
        text: str | None = None
        missing = ""
        ext = path.suffix.lower()
        try:
            if ref.size == 0 or ref.size > MAX_FILE_FOR_EXTRACT and ext == ".pdf":
                text = None
            elif ext in TEXT_EXT or mime.startswith("text/"):
                text = _read_head(path)
            elif ext in OFFICE_ZIP and ref.size <= MAX_FILE_FOR_EXTRACT:
                text = _office_text(path, OFFICE_ZIP[ext])
            elif ext == ".pdf":
                if shutil.which("pdftotext") is None:
                    missing = "pdftotext"
                else:                                         # the descriptor is opened without following symlinks and handed to the tool
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    try:
                        r = subprocess.run(["pdftotext", "-l", "2", "-q", "--", f"/dev/fd/{fd}", "-"], pass_fds=(fd,),
                                           capture_output=True, timeout=15, preexec_fn=_limits, check=False)
                    finally:
                        os.close(fd)
                    text = r.stdout[:HEAD_BYTES].decode("utf-8", errors="replace") if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            text = None
        if text is not None and not text.strip():
            text = None
        if looks_sensitive(path.name, text):
            return Evidence(ref, path.name, mime, None, metadata={"sensitive": "1"})
        return Evidence(ref, path.name, mime, text, metadata={"missing_tool": missing} if missing else {})
