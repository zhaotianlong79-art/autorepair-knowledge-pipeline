"""Deterministic section chunks with lossless source spans and explicit context links.

Reading-order links describe context, not a claim that a warning applies to a repair.
Atomic tables, pictures, captions and callouts may exceed the configured character cap.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import unquote

from pydantic import Field

from autorepair.models import Document, Model

ALGORITHM = "section/1"
WARNING = re.compile(
    r"^\s*(?:(?:WARNING|CAUTION|DANGER|NOTICE|NOTE|HINT)\b|警告|注意|危险|提示)", re.I
)
NUMBER = re.compile(r"^\d+[.)、]$")


@dataclass(frozen=True)
class ChunkConfig:
    target_chars: int = 800
    max_chars: int = 1200

    def __post_init__(self):
        if (
            type(self.target_chars) is not int
            or type(self.max_chars) is not int
            or not 32 <= self.target_chars <= self.max_chars
        ):
            raise ValueError("Require 32 <= target_chars <= max_chars")

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.metadata, sort_keys=True).encode()).hexdigest()

    @property
    def metadata(self):
        return {"algorithm": ALGORITHM, **asdict(self)}


DEFAULT = ChunkConfig()


class ChunkPart(Model):
    block_id: str
    kind: str
    start: int
    end: int
    text: str
    location: dict = Field(default_factory=dict)
    image_src: str = ""
    marker: str = ""
    list_depth: int = 0
    is_warning: bool = False
    atomic: bool = False
    links: list[str] = Field(default_factory=list)
    anchor: str = ""


class ContextLink(Model):
    block_id: str
    reason: str


class Chunk(Model):
    chunk_id: str
    doc_id: str
    brand: str
    section_id: str
    section_path: list[str]
    source_block_ids: list[str]
    step_ids: list[str]
    parts: list[ChunkPart]
    text: str
    pages: list[int]
    related: list[ContextLink]
    config: dict
    config_hash: str
    text_mode: str
    oversized: bool


def warning(block):
    return block.kind == "callout" or bool(WARNING.match(block.text))


def marker(block):
    return block.list_marker or (f"{block.ordinal}." if block.ordered else "-")


def step(block):
    return block.kind == "list" and (block.ordered or bool(NUMBER.fullmatch(marker(block))))


def display(part):
    if part.kind == "list" and part.start == 0:
        return "    " * part.list_depth + part.marker + " " + part.text
    return part.text


def split_ranges(text, limit):
    """Keep every character, including whitespace; offsets refer to selected source text."""
    start = 0
    while len(text) - start > limit:
        end = start + limit
        candidates = [m.end() for m in re.finditer(r"[\n。！？.!?;；]|\s+", text[start:end])]
        cut = next((x for x in reversed(candidates) if x >= limit // 2), limit)
        yield start, start + cut
        start += cut
    yield start, len(text)


def sections(document):
    """Use actual heading boundaries; unlabelled documents fall back to supplied paths."""
    groups = []
    stack = []
    current = None
    for block in document.blocks:
        is_heading = block.kind == "heading" and not warning(block)
        continued = (
            is_heading
            and stack
            and re.search(r"\(continued\)|（续）|\(续\)", block.text, re.I)
            and re.sub(
                r"\s*(?:\(continued\)|（续）|\(续\))\s*", "", block.text, flags=re.I
            ).casefold()
            == stack[-1][2].strip().casefold()
        )
        if continued:
            is_heading = False
        if is_heading:
            stack = [x for x in stack if x[0] < block.heading_level]
            stack.append((block.heading_level, block.block_id, block.text))
        path = (
            [x[2] for x in stack]
            if stack
            else (block.section_path or ([block.section] if block.section else []))
        )
        # Metadata-only paths are respected when a source has no heading blocks.
        if current is None or is_heading or (not stack and current["path"] != path):
            current = dict(
                id=block.block_id, path=path, ancestors=[x[1] for x in stack[:-1]], blocks=[]
            )
            groups.append(current)
        current["blocks"].append(block)
    return groups


def chunk_document(
    document: Document, config: ChunkConfig = DEFAULT, *, original=False
) -> list[Chunk]:
    ids = [b.block_id for b in document.blocks]
    if len(ids) != len(set(ids)):
        raise ValueError("Cannot chunk duplicate source block IDs")
    chunks = []
    anchors = {}
    for b in document.blocks:
        if b.anchor:
            anchors.setdefault(b.anchor, []).append(b.block_id)
    references = {}
    for b in document.blocks:
        for target in b.links:
            fragment = (
                target[1:]
                if target.startswith("#")
                else target.removeprefix(f"doc:{document.doc_id}#")
                if target.startswith(f"doc:{document.doc_id}#")
                else ""
            )
            resolved = anchors.get(unquote(fragment), [])
            if len(resolved) == 1:
                references.setdefault(b.block_id, []).append(resolved[0])
    global_warnings = {}
    mode = "original" if original else "normalized"
    for section in sections(document):
        blocks = section["blocks"]
        top_depth = min((b.list_depth for b in blocks if step(b)), default=0)
        # Before the first root step, callouts are section context, inherited by descendants.
        first_step = next(
            (i for i, b in enumerate(blocks) if step(b) and b.list_depth == top_depth), len(blocks)
        )
        local_warnings = [b.block_id for b in blocks[:first_step] if warning(b)]
        global_warnings[section["id"]] = local_warnings
        scoped_warnings = local_warnings + [
            w for a in section["ancestors"] for w in global_warnings.get(a, [])
        ]
        step_groups = []
        active = []
        step_id = ""
        for block in blocks:
            if step(block) and block.list_depth == top_depth and not warning(block):
                if active:
                    step_groups.append((step_id, active))
                active = []
                step_id = block.block_id
            active.append(block)
        if active:
            step_groups.append((step_id, active))
        # Only adjacent reading-order neighbors are inferred for visual/parameter context.
        neighbors = {}
        for i, block in enumerate(blocks):
            if block.kind in {"table", "picture", "caption"}:
                for j in [i - 1, i + 1]:
                    if 0 <= j < len(blocks) and blocks[j].kind not in {"heading", "running_header"}:
                        other = blocks[j]
                        # Do not attach a visual item to a different root step.
                        if step(other) and j > i:
                            continue
                        neighbors.setdefault(block.block_id, []).append(other.block_id)
                        neighbors.setdefault(other.block_id, []).append(block.block_id)
        for step_id, group in step_groups:
            packed = []
            units = []
            for block in group:
                text = block.original if original else block.text
                atomic = block.kind in {"table", "picture", "caption", "callout"} or warning(block)
                overhead = (
                    len(marker(block)) + 1 + 4 * block.list_depth if block.kind == "list" else 0
                )
                atomic = atomic or (block.kind == "list" and overhead >= config.max_chars)
                spans = (
                    [(0, len(text))]
                    if atomic
                    else split_ranges(text, max(1, config.max_chars - overhead))
                )
                for start, end in spans:
                    part = ChunkPart(
                        block_id=block.block_id,
                        kind=block.kind,
                        start=start,
                        end=end,
                        text=text[start:end],
                        location=block.location,
                        image_src=block.image_src,
                        marker=marker(block) if block.kind == "list" else "",
                        list_depth=block.list_depth,
                        is_warning=warning(block),
                        atomic=atomic,
                        links=block.links,
                        anchor=block.anchor,
                    )
                    size = len("\n\n".join(display(p) for p in [*packed, part]))
                    if packed and size > config.max_chars:
                        units.append(packed)
                        packed = []
                    packed.append(part)
                    if len("\n\n".join(display(p) for p in packed)) >= config.target_chars:
                        units.append(packed)
                        packed = []
            if packed:
                units.append(packed)
            for parts in units:
                owned = list(dict.fromkeys(p.block_id for p in parts))
                links = {(b, "section-warning") for b in scoped_warnings if b not in owned}
                if step_id:
                    links.update(
                        (b.block_id, "step-context") for b in group if b.block_id not in owned
                    )
                if step_id and any(p.block_id == step_id and p.start > 0 for p in parts):
                    links.add((step_id, "step-context"))
                for b in owned:
                    links.update(
                        (target, "explicit-reference")
                        for target in references.get(b, [])
                        if target not in owned
                    )
                    links.update(
                        (other, "adjacent-context")
                        for other in neighbors.get(b, [])
                        if other not in owned
                    )
                text = "\n\n".join(display(p) for p in parts)
                identity = [
                    document.doc_id,
                    document.brand,
                    document.source_hash,
                    section["id"],
                    section["path"],
                    sorted(links),
                    config.digest,
                    mode,
                    [p.model_dump() for p in parts],
                    text,
                ]
                digest = hashlib.sha256(
                    json.dumps(identity, ensure_ascii=False).encode()
                ).hexdigest()
                chunks.append(
                    Chunk(
                        chunk_id="chunk-" + digest[:24],
                        doc_id=document.doc_id,
                        brand=document.brand,
                        section_id=section["id"],
                        section_path=section["path"],
                        source_block_ids=owned,
                        step_ids=[step_id] if step_id else [],
                        parts=parts,
                        text=text,
                        pages=sorted({pn for p in parts for pn in p.location.get("pages", [])}),
                        related=[
                            ContextLink(block_id=b, reason=reason) for b, reason in sorted(links)
                        ],
                        config=config.metadata,
                        config_hash=config.digest,
                        text_mode=mode,
                        oversized=len(text) > config.max_chars,
                    )
                )
    return chunks
