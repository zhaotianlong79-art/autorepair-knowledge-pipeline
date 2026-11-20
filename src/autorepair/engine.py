"""Deterministic brand-aware rules and guarded changes."""

import re
from collections import Counter
from collections.abc import Mapping

from autorepair.config import Config
from autorepair.models import Change, Document, Issue, Result
from autorepair.privacy import redact

PROTECTED = re.compile(
    r"\bP\d{4}\b|\bPN-[A-Z0-9-]+\b|(?<![\w.])-?\d+(?:\.\d+)?(?:\s*[-~至]\s*\d+(?:\.\d+)?)?\s*(?:N[· ]?m|Nm|mm|kPa|MPa|°C|V|A|L|ml)\b|不得|禁止|切勿|不要|警告|危险|\b(?:WARNING|DANGER|not|never)\b",
    re.I,
)


def signature(text: str) -> list[str]:
    return [m.group().casefold() for m in PROTECTED.finditer(text)]


def clean_structure(text: str) -> str:
    return "\n".join(
        re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()
    ).strip()


def replace_terms(text: str, cfg: Config, kind: str) -> str:
    mapping = {
        alias.casefold(): t.canonical for t in cfg.terms if t.kind == kind for alias in t.aliases
    }
    if not mapping:
        return text
    protected_spans = [(m.start(), m.end()) for m in PROTECTED.finditer(text)]
    for term in cfg.protected_terms:
        protected_spans.extend(
            (m.start(), m.end()) for m in re.finditer(re.escape(term), text, re.I)
        )
    # Canonical terms remain atomic, including when an alias is inside a canonical term.
    canonical_spans = [
        (m.start(), m.end())
        for t in cfg.terms
        for m in re.finditer(re.escape(t.canonical), text, re.I)
    ]
    pattern = re.compile(
        "|".join(re.escape(a) for a in sorted(mapping, key=len, reverse=True)), re.I
    )

    def substitute(match):
        start, end = match.span()
        if any(start < b and end > a for a, b in protected_spans + canonical_spans):
            return match.group()
        if (
            match.group()[0].isascii()
            and start
            and (
                text[start - 1].isascii() and (text[start - 1].isalnum() or text[start - 1] == "_")
            )
        ):
            return match.group()
        if (
            match.group()[-1].isascii()
            and end < len(text)
            and (text[end].isascii() and (text[end].isalnum() or text[end] == "_"))
        ):
            return match.group()
        return mapping[match.group().casefold()]

    return pattern.sub(substitute, text)


def scrub_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub_value(x) for x in value]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key = str(key)
            if key.casefold() in {"电话", "手机", "phone", "mobile", "车牌", "plate"}:
                contextual = f"{key}: {item}"
                masked = redact(contextual)
                if contextual != masked:
                    cleaned[key] = masked.split(": ", 1)[1]
                    continue
            cleaned[redact(key)] = scrub_value(item)
        return cleaned
    return value


def process(
    document: Document, cfg: Config, corpus: Mapping[str, set[str]] | None = None
) -> Result:
    if document.brand != cfg.brand:
        raise ValueError("Document/config brand mismatch")
    doc = document.model_copy(deep=True)
    result = Result(document=doc, rules_hash=cfg.digest, issues=list(doc.issues))
    # Sanitize metadata as well as public before/after views. Raw input stays only in its source file.
    doc.source_name = redact(doc.source_name)
    for block in doc.blocks:
        block.location = scrub_value(block.location)
        block.links = [redact(link) for link in block.links]
        block.section = redact(block.section)
    for rule in cfg.rules:
        for block in doc.blocks:
            before = block.text
            after = before
            if rule.action == "structure":
                after = clean_structure(before)
            elif rule.action in {"terms", "tools", "components", "long_tail"}:
                kind = {
                    "terms": "term",
                    "tools": "tool",
                    "components": "component",
                    "long_tail": "long_tail",
                }[rule.action]
                after = replace_terms(before, cfg, kind)
            elif rule.action == "style":
                after = before.replace("！！", "！")
            elif rule.action == "pii":
                after = redact(before)
            elif rule.action == "retrieval":
                after = before.strip()
            if signature(redact(before)) != signature(redact(after)):
                result.issues.append(
                    Issue(
                        code="guard.protected",
                        severity="error",
                        message=f"规则 {rule.id} 尝试修改关键参数或警告词，已拒绝",
                        block_id=block.block_id,
                    )
                )
                continue
            if before != after:
                result.changes.append(
                    Change(
                        rule_id=rule.id,
                        rule_version=rule.version,
                        block_id=block.block_id,
                        before=redact(before),
                        after=redact(after),
                    )
                )
                block.text = after
    # Original fields are redacted even if the privacy stage was disabled in a custom configuration.
    for block in doc.blocks:
        block.original = redact(block.original)
        block.text = redact(block.text)
    doc.component_mentions = []
    for block in doc.blocks:
        # Store evidence, not an inferred global part identity. Unknown applicability stays unknown.
        for line in block.text.splitlines():
            parts = re.findall(r"\bPN-[A-Z0-9-]+\b", line)
            components = [
                t.canonical for t in cfg.terms if t.kind == "component" and t.canonical in line
            ]
            for part in parts:
                doc.component_mentions.append(
                    {
                        "part_number": part,
                        "descriptions": components,
                        "evidence": line,
                        "block_id": block.block_id,
                        "location": block.location,
                        "model": doc.model,
                        "year_range": doc.year_range,
                    }
                )
    if not doc.blocks or not any(b.text.strip() for b in doc.blocks):
        result.issues.append(
            Issue(code="content.empty", severity="error", message="文档没有可用正文")
        )
    if doc.expected_blocks is not None and len(doc.blocks) != doc.expected_blocks:
        result.issues.append(
            Issue(
                code="content.missing", severity="error", message="结构化文档块数量与来源记录不一致"
            )
        )
    ids = [b.block_id for b in doc.blocks]
    if len(set(ids)) != len(ids):
        result.issues.append(
            Issue(code="structure.duplicate_id", severity="error", message="存在重复块 ID")
        )
    counts = Counter(b.text for b in doc.blocks if b.text)
    for count in counts.values():
        if count > 1:
            result.issues.append(
                Issue(
                    code="content.duplicate",
                    severity="warning",
                    message=f"发现 {count} 个相同内容块，保留原位置供复核",
                )
            )
    known = (
        {"WARNING", "DANGER", "ABS", "Nm", "PN"} | set(cfg.known_terms) | set(cfg.protected_terms)
    )
    for term in cfg.terms:
        known.update([term.canonical, *term.aliases])
    for block in doc.blocks:
        if not block.location:
            result.issues.append(
                Issue(
                    code="source.missing",
                    severity="warning",
                    message="缺少来源定位",
                    block_id=block.block_id,
                )
            )
        # Bounded unknown acronym detector, not a claim of complete terminology discovery.
        unknown = (
            set(
                re.findall(
                    r"\b[A-Z]{2,6}\b", block.text.replace("[PHONE]", "").replace("[PLATE]", "")
                )
            )
            - known
        )
        unknown = {x for x in unknown if not re.search(rf"PN-{re.escape(x)}", block.text)}
        if unknown:
            result.issues.append(
                Issue(
                    code="term.unknown",
                    severity="warning",
                    message="未登记缩写: " + ", ".join(sorted(unknown)),
                    block_id=block.block_id,
                )
            )
        for link in block.links:
            if not (link.startswith("#") or link.startswith("doc:")):
                continue
            if link.startswith("#"):
                target, section = doc.doc_id, link[1:]
            else:
                target, _, section = link[4:].partition("#")
            registry = (
                corpus
                if corpus is not None
                else {doc.doc_id: {b.section for b in doc.blocks if b.section}}
            )
            if target not in registry or (section and section not in registry[target]):
                result.issues.append(
                    Issue(
                        code="reference.broken",
                        severity="warning",
                        message=f"引用目标不可解析: {link}",
                        block_id=block.block_id,
                    )
                )
    # PII stage's before view intentionally contains no unredacted value.
    result.issues = [Issue.model_validate(scrub_value(i.model_dump())) for i in result.issues]
    doc.issues = [Issue.model_validate(scrub_value(i.model_dump())) for i in doc.issues]
    return result.finalize()
