"""Explicit applicability, validated input and conservative compound filters."""

import re

from pydantic import Field, model_validator

from autorepair.models import Applicability, Document, DocumentType, Model
from autorepair.privacy import redact

FIELDS = ("model", "year_range", "document_type", "document_version", "applicability")
TYPE_LABELS = {
    "unknown": "未知",
    "manual": "维修手册",
    "service_bulletin": "维修通报",
    "procedure": "操作流程",
    "wiring_diagram": "电路图",
    "parts_catalog": "零件目录",
    "training": "培训资料",
    "other": "其他",
}


def text(value):
    return " ".join(value.strip().split()) or "unknown"


def years(value):
    value = text(value)
    if value.casefold() == "unknown":
        return None, None
    m = re.fullmatch(r"(\d{4})(?:\s*[-–—]\s*(\d{4}))?", value)
    if not m:
        raise ValueError("年份须为 YYYY、YYYY-YYYY 或 unknown")
    start, end = int(m[1]), int(m[2] or m[1])
    Applicability(model="unknown", year_start=start, year_end=end)
    return start, end


def range_label(scope):
    return (
        "unknown"
        if scope.year_start is None
        else str(scope.year_start)
        if scope.year_start == scope.year_end
        else f"{scope.year_start}-{scope.year_end}"
    )


def scopes(document):
    if document.applicability:
        return document.applicability
    try:
        start, end = years(document.year_range)
    except ValueError:
        start, end = None, None  # Unparseable legacy dates never mean "all years".
    return [Applicability(model=document.model or "unknown", year_start=start, year_end=end)]


def snapshot(document):
    return {
        k: getattr(document, k)
        if k != "applicability"
        else [s.model_dump() for s in document.applicability]
        for k in FIELDS
    }


class MetadataPatch(Model):
    model: str | None = None
    year_range: str | None = None
    document_type: DocumentType | None = None
    document_version: str | None = None
    applicability: list[Applicability] | None = None

    @model_validator(mode="after")
    def validate_input(self):
        if self.applicability is not None and (
            self.model is not None or self.year_range is not None
        ):
            raise ValueError("适用范围不能与单车型／年份字段同时录入")
        if self.year_range is not None:
            years(self.year_range)
        return self

    @property
    def supplied(self):
        return {k: v for k, v in self.model_dump(exclude_none=True).items()}


def apply_metadata(document: Document, patch: MetadataPatch):
    doc = document.model_copy(deep=True)
    if patch.model is not None or patch.year_range is not None:
        if len(doc.applicability) > 1:
            raise ValueError("多车型资料请使用完整适用范围修正，避免破坏车型与年份的对应关系")
        old = scopes(doc)[0]
        start, end = (
            years(patch.year_range)
            if patch.year_range is not None
            else (old.year_start, old.year_end)
        )
        doc.applicability = [
            Applicability(
                model=redact(text(patch.model)) if patch.model is not None else old.model,
                year_start=start,
                year_end=end,
            )
        ]
    if patch.applicability is not None:
        doc.applicability = [
            s.model_copy(update={"model": redact(s.model)}) for s in patch.applicability
        ]
    if patch.applicability is not None or patch.model is not None or patch.year_range is not None:
        doc.model = " / ".join(dict.fromkeys(s.model for s in doc.applicability)) or "unknown"
        doc.year_range = (
            "; ".join(dict.fromkeys(range_label(s) for s in doc.applicability)) or "unknown"
        )
    if patch.document_type is not None:
        doc.document_type = patch.document_type
    if patch.document_version is not None:
        doc.document_version = redact(text(patch.document_version))
    for mention in doc.component_mentions:
        mention.update(
            model=doc.model,
            year_range=doc.year_range,
            applicability=[s.model_dump() for s in scopes(doc)],
        )
    return doc


class MetadataFilter(Model):
    model: str | None = None
    year_start: int | None = Field(default=None, ge=1900, le=2200, strict=True)
    year_end: int | None = Field(default=None, ge=1900, le=2200, strict=True)
    document_type: DocumentType | None = None
    document_version: str | None = None
    include_unknown: bool = False

    @model_validator(mode="after")
    def validate_range(self):
        if (self.year_start is None) != (self.year_end is None) or (
            self.year_start is not None and self.year_start > self.year_end
        ):
            raise ValueError("查询年份范围须完整且起始年不大于结束年")
        return self

    def match(self, document):
        unknown = False
        for key in ["document_type", "document_version"]:
            required = getattr(self, key)
            actual = getattr(document, key)
            if required is not None and text(actual) != text(required):
                if text(actual).casefold() == "unknown" and self.include_unknown:
                    unknown = True
                else:
                    return None
        matches = []
        for scope in scopes(document):
            uncertain = unknown
            if (
                self.model is not None
                and text(scope.model).casefold() != text(self.model).casefold()
            ):
                if scope.model.casefold() == "unknown" and self.include_unknown:
                    uncertain = True
                else:
                    continue
            if self.year_start is not None:
                if scope.year_start is None:
                    if not self.include_unknown:
                        continue
                    uncertain = True
                elif scope.year_start > self.year_end or scope.year_end < self.year_start:
                    continue
            matches.append((uncertain, scope.model_dump()))
        if not matches:
            return None
        known = [s for u, s in matches if not u]
        return {
            "filtered": any(
                getattr(self, k) is not None
                for k in ["model", "year_start", "document_type", "document_version"]
            ),
            "uses_unknown": not bool(known),
            "matched_applicability": known or [s for _, s in matches],
        }


def load_manifest(path, sources):
    """Per-file patches relative to a manifest, restricted to files in this batch."""
    import json
    from pathlib import Path

    manifest = Path(path)
    data = json.loads(manifest.read_text())
    if not isinstance(data, dict):
        raise ValueError("元数据清单须为 文件相对路径 → 元数据对象 的 JSON 对象")
    allowed = {str(p.resolve()) for p in sources}
    result = {}
    for name, values in data.items():
        resolved = str((manifest.parent / name).resolve())
        if resolved not in allowed or resolved in result:
            raise ValueError(f"清单文件不在当前批次或重复：{name}")
        result[resolved] = MetadataPatch.model_validate(values)
    return result
