#!/usr/bin/env python3
"""Verify that volatile facts restated in documentation still match their source.

文档复述的易变事实必须与唯一真值源保持一致。

Checks
------
1. Every numbered "current session schema" claim in current documentation equals
   ``SCHEMA_VERSION`` in the persistence constants module.
2. Each canonical compatibility matrix file states that version exactly once, so
   each language has one authoritative home instead of many that drift apart.
3. Sanctioned ``src/neuro_code/...`` paths and ``neuro_code.<module>``
   references in current documentation resolve to real files or modules.
4. Volatile counts (test totals, documentation pairs) carry a date or an
   explicit historical marker rather than silently claiming to be current.
5. ADR numbers are unique, contiguous, and matched by their own titles, so a
   bare "ADR 0074" reference can never be ambiguous and a next number is always
   obvious.

Exempt from every check:

``docs/*/adr/``
    An architecture decision record is an immutable historical record. It is
    expected to name removed modules and superseded schema revisions.
``docs/agent_memory/``
    A local, git-ignored handoff directory. It is covered by its own gate
    because its "current state" and "timeline archive" files need different
    rules and it is absent from a clean CI checkout.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DOCS_ROOT = _REPOSITORY_ROOT / "docs"
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"

_SCHEMA_CONSTANTS = (
    _REPOSITORY_ROOT
    / "src"
    / "neuro_code"
    / "infrastructure"
    / "persistence"
    / "sqlite_session_constants.py"
)

# The only documentation files allowed to state the current schema version.
_SCHEMA_AUTHORITY = (
    Path("en") / "compatibility-matrix.md",
    Path("zh-CN") / "compatibility-matrix.md",
)

_EXEMPT_DIRECTORY_NAMES = frozenset({"adr", "agent_memory"})

# Word boundaries matter: a naive case-insensitive "current schema" search also
# matches the English word "Concurrent schema".
_SCHEMA_CLAIM_PATTERNS = (
    re.compile(r"\bcurrent Session Store schema is (?P<version>\d+)", re.IGNORECASE),
    re.compile(r"\bcurrent Session schema (?P<version>\d+)", re.IGNORECASE),
    re.compile(r"\bcurrent schema (?P<version>\d+)", re.IGNORECASE),
    re.compile(r"\bcurrent development schema v(?P<version>\d+)", re.IGNORECASE),
    re.compile(r"当前 Session Store schema 为 (?P<version>\d+)"),
    re.compile(r"当前 Session schema (?P<version>\d+)"),
    re.compile(r"当前 schema (?P<version>\d+)"),
    re.compile(r"当前开发 schema v(?P<version>\d+)"),
)

_HISTORICAL_MARKERS = (
    "former",
    "formerly",
    "removed",
    "no longer",
    "historical",
    "obsolete",
    "superseded",
    "was deleted",
    "there is no",
    "no top-level",
    "已移除",
    "已删除",
    "不再",
    "历史",
    "当时",
    "不存在",
    "没有顶层",
)

# Prose wraps mid-sentence, so a removal marker often sits on a neighbouring
# line rather than the line that names the module.
_CONTEXT_WINDOW = 2

_DATE_PATTERN = re.compile(r"\b20\d\d-\d\d-\d\d\b")

_SOURCE_PATH_PATTERN = re.compile(r"src/neuro_code/[A-Za-z0-9_./-]+?\.py\b")
_SOURCE_DIRECTORY_PATTERN = re.compile(r"src/neuro_code/[A-Za-z0-9_./-]+/")
_MODULE_REFERENCE_PATTERN = re.compile(r"\bneuro_code(?:\.[a-z_][a-z0-9_]*)+")

_COUNT_PATTERNS = (
    re.compile(r"\b\d[\d,]* bilingual pairs\b"),
    re.compile(r"\b\d[\d,]* (?:pytest )?tests\b"),
)

_ADR_FILENAME_PATTERN = re.compile(r"^\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
# Records in the earliest series separate the number from the title with an
# em dash, so the separator is deliberately permissive. Escapes keep this line
# ASCII: a literal fullwidth colon or en dash trips RUF001.
_ADR_TITLE_PATTERN = re.compile("^#\\s*ADR\\s*(?P<number>\\d{4})\\s*[:\uff1a\u2014\u2013-]")
_ADR_STATUS_PATTERN = re.compile(
    "^-\\s*(?:Status|状态)\\s*[:\uff1a]\\s*(?P<value>.+)$", re.MULTILINE
)
_ADR_DATE_PATTERN = re.compile("^-\\s*(?:Date|日期)\\s*[:\uff1a]\\s*(?P<value>.+)$", re.MULTILINE)

_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+?)(?:\s+\"[^\"]*\")?\)")
_FENCE_PATTERN = re.compile(r"^\s*(?:```|~~~)")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")
_HEADING_PATTERN = re.compile(r"^#{2,3}\s")
_STANDARD_SECTIONS = (
    "Context",
    "Decision",
    "Consequences",
    "Validation",
    "Status",
    "Non-goals",
    "References",
    "Boundaries",
)
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class Finding:
    """One documentation inconsistency."""

    path: Path
    line_number: int
    message: str


def _read_schema_version() -> int:
    text = _SCHEMA_CONSTANTS.read_text(encoding="utf-8")
    match = re.search(r"^SCHEMA_VERSION = (?P<version>\d+)\s*$", text, re.MULTILINE)
    if match is None:
        raise SystemExit(f"cannot read SCHEMA_VERSION from {_SCHEMA_CONSTANTS}")
    return int(match.group("version"))


def _is_exempt(path: Path) -> bool:
    relative = path.relative_to(_DOCS_ROOT)
    return any(part in _EXEMPT_DIRECTORY_NAMES for part in relative.parts[:-1])


def _checked_documents() -> list[Path]:
    """Return the current-state documents, newest path first."""

    return sorted(
        path for path in _DOCS_ROOT.rglob("*.md") if path.is_file() and not _is_exempt(path)
    )


def _is_historical(text: str) -> bool:
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in _HISTORICAL_MARKERS) or bool(
        _DATE_PATTERN.search(text)
    )


def _check_schema_claims(version: int) -> list[Finding]:
    findings: list[Finding] = []
    authority_counts: dict[Path, int] = dict.fromkeys(_SCHEMA_AUTHORITY, 0)
    for path in _checked_documents():
        relative = path.relative_to(_DOCS_ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in _SCHEMA_CLAIM_PATTERNS:
                for match in pattern.finditer(line):
                    stated = int(match.group("version"))
                    if stated != version:
                        findings.append(
                            Finding(
                                path=relative,
                                line_number=line_number,
                                message=(
                                    f"states current session schema {stated}, "
                                    f"but SCHEMA_VERSION is {version}"
                                ),
                            )
                        )
                    if relative in authority_counts:
                        authority_counts[relative] += 1
                    else:
                        findings.append(
                            Finding(
                                path=relative,
                                line_number=line_number,
                                message=(
                                    "states a numbered current schema version outside "
                                    f"{' and '.join(str(item) for item in _SCHEMA_AUTHORITY)}; "
                                    "drop the number and let the authoritative row carry it"
                                ),
                            )
                        )
    for authority, count in authority_counts.items():
        if count != 1:
            findings.append(
                Finding(
                    path=authority,
                    line_number=0,
                    message=(f"must state the current schema version exactly once, found {count}"),
                )
            )
    return findings


def _resolves_as_module(reference: str) -> bool:
    parts = reference.split(".")
    if not parts or parts[0] != "neuro_code":
        return False
    base = _SOURCE_ROOT.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _resolve_reference(reference: str) -> bool:
    """Return whether a dotted reference names a module or a symbol in one."""

    parts = reference.split(".")
    last = parts[-1]
    if last[:1].isupper() or (last.startswith("__") and last.endswith("__")):
        # A trailing capitalised or dunder component names a symbol, not a module.
        parts = parts[:-1]
    return bool(parts) and _resolves_as_module(".".join(parts))


def _check_paths() -> list[Finding]:
    findings: list[Finding] = []
    for path in _checked_documents():
        relative = path.relative_to(_DOCS_ROOT)
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            window = "\n".join(lines[max(0, index - _CONTEXT_WINDOW) : index + _CONTEXT_WINDOW + 1])
            if _is_historical(window):
                continue
            references: set[str] = set(_SOURCE_PATH_PATTERN.findall(line))
            references.update(_SOURCE_DIRECTORY_PATTERN.findall(line))
            references.update(_MODULE_REFERENCE_PATTERN.findall(line))
            for reference in sorted(references):
                if reference.startswith("src/neuro_code/"):
                    if not (_REPOSITORY_ROOT / reference.rstrip("/")).exists():
                        findings.append(
                            Finding(
                                path=relative,
                                line_number=index + 1,
                                message=f"references missing path {reference}",
                            )
                        )
                    continue
                if not _resolve_reference(reference):
                    findings.append(
                        Finding(
                            path=relative,
                            line_number=index + 1,
                            message=f"references missing module {reference}",
                        )
                    )
    return findings


def _headings(path: Path, levels: re.Pattern[str]) -> list[str]:
    """Return heading lines at the requested levels, ignoring fenced code."""

    found: list[str] = []
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE_PATTERN.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and levels.match(line):
            found.append(line.strip())
    return found


def _slugify(title: str) -> str:
    text = re.sub(r"[`*_]", "", title.strip().lower())
    text = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def _check_links() -> list[Finding]:
    """Every relative Markdown link must resolve, including its anchor.

    文档内部的相对链接必须真实可达。这条检查发现的是一类真实缺陷:
    记录写在 ``adr/`` 目录里却把兄弟记录链接成 ``adr/xxxx-....md``。
    """

    findings: list[Finding] = []
    documents = sorted(_DOCS_ROOT.rglob("*.md"))
    anchors = {
        path.resolve(): {
            _slugify(heading.lstrip("#").strip())
            for heading in _headings(path, re.compile(r"^#{1,6}\s"))
        }
        for path in documents
    }
    for path in documents:
        relative = path.relative_to(_DOCS_ROOT)
        in_fence = False
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _FENCE_PATTERN.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in _MARKDOWN_LINK_PATTERN.finditer(line):
                target = match.group("target")
                if target.startswith(_EXTERNAL_PREFIXES):
                    continue
                location, _, fragment = target.partition("#")
                resolved = (path.parent / location).resolve() if location else path.resolve()
                if location and not resolved.exists():
                    findings.append(
                        Finding(
                            path=relative,
                            line_number=line_number,
                            message=f"link target does not exist: {target}",
                        )
                    )
                    continue
                if fragment and resolved in anchors and fragment not in anchors[resolved]:
                    findings.append(
                        Finding(
                            path=relative,
                            line_number=line_number,
                            message=f"link anchor does not exist: {target}",
                        )
                    )
    return findings


def _check_decision_record_template() -> list[Finding]:
    """Both languages of a record must share structure and complete metadata.

    中英文 ADR 必须章节数一致、元数据完整:语言切换行、以规范词开头的状态、以及日期。
    缺任何一项都会让记录无法被检索或对照。
    """

    findings: list[Finding] = []
    for language, status_word, date_label in (
        ("en", "Accepted", "Date"),
        ("zh-CN", "已接受", "日期"),
    ):
        directory = _DOCS_ROOT / language / "adr"
        counterpart_language = "zh-CN" if language == "en" else "en"
        for path in sorted(directory.glob("*.md")):
            relative = path.relative_to(_DOCS_ROOT)
            text = path.read_text(encoding="utf-8")

            if f"](../../{counterpart_language}/adr/" not in text:
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message=f"missing the {counterpart_language} language-switch line",
                    )
                )
            status = _ADR_STATUS_PATTERN.search(text)
            if status is None:
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message=f"missing '- {status_word if language == 'en' else '状态'}' metadata",
                    )
                )
            elif not status.group("value").strip().startswith(status_word):
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message=(
                            f"status must start with {status_word!r}, found "
                            f"{status.group('value').strip()[:40]!r}"
                        ),
                    )
                )
            date = _ADR_DATE_PATTERN.search(text)
            if date is None:
                findings.append(
                    Finding(
                        path=relative, line_number=0, message=f"missing '- {date_label}:' metadata"
                    )
                )
            elif not _DATE_PATTERN.fullmatch(date.group("value").strip()):
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message="date must use the ISO 8601 YYYY-MM-DD form",
                    )
                )

            counterpart = _DOCS_ROOT / counterpart_language / "adr" / path.name
            if not counterpart.is_file():
                continue
            own = _headings(path, _HEADING_PATTERN)
            other = _headings(counterpart, _HEADING_PATTERN)
            if len(own) != len(other):
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message=(
                            f"has {len(own)} section headings but its {counterpart_language} "
                            f"counterpart has {len(other)}"
                        ),
                    )
                )
            if language == "zh-CN":
                stray = [h for h in own if h[3:].strip() in _STANDARD_SECTIONS]
                if stray:
                    findings.append(
                        Finding(
                            path=relative,
                            line_number=0,
                            message=f"Chinese record keeps English section headings: {stray}",
                        )
                    )
            elif any(_CJK_PATTERN.search(h) for h in own):
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message="English record contains a Chinese section heading",
                    )
                )
    return findings


def _check_decision_record_numbering() -> list[Finding]:
    """Keep ADR numbers unique, contiguous, and matched by their title.

    保持 ADR 编号唯一且连续,并要求标题编号与文件名一致。编号重复会让
    "ADR 0074" 这类引用产生歧义,而缺号会让下一个编号无从判断。
    """

    findings: list[Finding] = []
    languages = ("en", "zh-CN")
    name_sets: dict[str, set[str]] = {}
    for language in languages:
        directory = _DOCS_ROOT / language / "adr"
        if not directory.is_dir():
            continue
        numbers: dict[str, Path] = {}
        names: set[str] = set()
        for path in sorted(directory.glob("*.md")):
            relative = path.relative_to(_DOCS_ROOT)
            names.add(path.name)
            prefix = path.name[:4]
            if not _ADR_FILENAME_PATTERN.match(path.name):
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message="ADR filename must look like 0000-lowercase-slug.md",
                    )
                )
                continue
            if prefix in numbers:
                findings.append(
                    Finding(
                        path=relative,
                        line_number=0,
                        message=(
                            f"duplicate ADR number {prefix}; already used by {numbers[prefix].name}"
                        ),
                    )
                )
            numbers[prefix] = path
            title_line = next(
                (
                    index
                    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                    if line.strip()
                ),
                None,
            )
            if title_line is None:
                findings.append(Finding(path=relative, line_number=0, message="ADR file is empty"))
                continue
            first_line = path.read_text(encoding="utf-8").splitlines()[title_line - 1]
            match = _ADR_TITLE_PATTERN.match(first_line)
            if match is None:
                findings.append(
                    Finding(
                        path=relative,
                        line_number=title_line,
                        message="first non-empty line must be '# ADR NNNN: <title>'",
                    )
                )
            elif match.group("number") != prefix:
                findings.append(
                    Finding(
                        path=relative,
                        line_number=title_line,
                        message=(
                            f"title numbers this record {match.group('number')} but the "
                            f"filename uses {prefix}"
                        ),
                    )
                )
        name_sets[language] = names
        if numbers:
            highest = max(int(number) for number in numbers)
            expected = {f"{number:04d}" for number in range(1, highest + 1)}
            for missing in sorted(expected - set(numbers)):
                findings.append(
                    Finding(
                        path=Path(language) / "adr",
                        line_number=0,
                        message=f"missing ADR number {missing}; numbers must be contiguous",
                    )
                )
    if len(name_sets) == len(languages) and name_sets["en"] != name_sets["zh-CN"]:
        findings.append(
            Finding(
                path=Path("en") / "adr",
                line_number=0,
                message="English and Chinese ADR filename sets differ",
            )
        )
    return findings


def _check_volatile_counts() -> list[Finding]:
    findings: list[Finding] = []
    for path in _checked_documents():
        relative = path.relative_to(_DOCS_ROOT)
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not any(pattern.search(line) for pattern in _COUNT_PATTERNS):
                continue
            window = "\n".join(lines[max(0, index - 2) : index + 3])
            if _is_historical(window):
                continue
            findings.append(
                Finding(
                    path=relative,
                    line_number=index + 1,
                    message=(
                        "states a volatile count without a date or historical marker; "
                        "counts drift, so date them or point at the producing script"
                    ),
                )
            )
    return findings


def main() -> int:
    version = _read_schema_version()
    documents = _checked_documents()
    findings = [
        *_check_schema_claims(version),
        *_check_paths(),
        *_check_volatile_counts(),
        *_check_links(),
        *_check_decision_record_numbering(),
        *_check_decision_record_template(),
    ]
    if findings:
        for finding in findings:
            location = f"docs/{finding.path}"
            if finding.line_number:
                location = f"{location}:{finding.line_number}"
            print(f"{location}: {finding.message}", file=sys.stderr)
        print(
            f"\ndocumentation consistency failed: {len(findings)} finding(s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"documentation consistency ok: schema version {version}, "
        f"{len(documents)} current-state documents checked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
