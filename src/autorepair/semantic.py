"""Local, pinned CLS embeddings and content-addressed derived vector cache.

The cache is never a publication authority: callers supply an approved, filtered
chunk snapshot on every query. No document text is sent to any remote service.
"""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Protocol


class SemanticError(ValueError):
    """Actionable dependency, model, input or derived-index error."""


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str = "BAAI/bge-small-zh-v1.5"
    revision: str = "7999e1d3359715c523056ef9478215996d62a620"
    dimension: int = 512
    query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    window_chars: int = 256
    overlap_chars: int = 48
    schema: str = "cls-l2-char-windows-v1"

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.model):
            raise ValueError("模型必须是 Hugging Face 仓库 ID")
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("模型 revision 必须固定为 40 位提交哈希")
        if not 1 <= self.dimension <= 8192:
            raise ValueError("无效向量维数")
        if not 0 <= self.overlap_chars < self.window_chars <= 400:
            raise ValueError("语义窗口须为 1–400 字符，重叠小于窗口")
        if self.schema != "cls-l2-char-windows-v1":
            raise ValueError("不支持的向量配置版本")

    @property
    def key(self):
        return digest(json.dumps(asdict(self), ensure_ascii=False, sort_keys=True))


DEFAULT_CONFIG = EmbeddingConfig()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def windows(text, config=DEFAULT_CONFIG):
    """Cover all characters, including the tail of oversized tables and warnings."""
    output = []
    start = 0
    while start < len(text):
        end = min(start + config.window_chars, len(text))
        output.append({"start": start, "end": end, "text": text[start:end]})
        if end == len(text):
            break
        start = end - config.overlap_chars
    return output or [{"start": 0, "end": 0, "text": ""}]


def chunk_text(chunk):
    return " / ".join(chunk.section_path) + "\n" + chunk.text


def identifier_filter(chunks, query):
    """Require complete DTC/part identifiers, rather than semantically near codes."""
    identifiers = re.findall(
        r"(?<![a-z0-9])(?:[pbcu][0-3][0-9a-f]{3}|(?=[a-z0-9-]*[0-9])[a-z0-9]{2,}(?:-[a-z0-9]+)+)(?![a-z0-9])",
        query,
        re.IGNORECASE,
    )
    return [
        c
        for c in chunks
        if all(
            re.search(
                r"(?<![a-z0-9-])" + re.escape(code) + r"(?![a-z0-9-])", chunk_text(c), re.IGNORECASE
            )
            for code in identifiers
        )
    ]


class Embedder(Protocol):
    """Replaceable local provider; config.key must uniquely identify its behavior."""

    config: EmbeddingConfig

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


class LocalEmbedder:
    def __init__(self, config=DEFAULT_CONFIG, *, download=False):
        self.config = config
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise SemanticError("请先执行 uv sync --extra semantic 安装本地语义检索依赖") from exc
        self.torch = torch
        options = dict(
            revision=config.revision,
            local_files_only=not download,
            trust_remote_code=False,
        )
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(config.model, **options)
            self.model = (
                AutoModel.from_pretrained(config.model, use_safetensors=True, **options)
                .to("cpu")
                .eval()
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise SemanticError(
                "无法加载固定版本的本地模型；首次请执行 autorepair semantic-model --download，检查模型缓存及依赖"
            ) from exc
        if self.model.config.hidden_size != config.dimension:
            raise SemanticError("模型维数与配置不一致，禁止混用向量")
        self.max_tokens = min(self.model.config.max_position_embeddings, 512)

    def encode(self, texts, *, query=False):
        output = []
        for start in range(0, len(texts), 16):
            batch = [
                (self.config.query_prefix if query else "") + t for t in texts[start : start + 16]
            ]
            # Never silently truncate. Character windows fit BGE's token budget;
            # a replacement model must also pass this explicit check.
            encoded = self.tokenizer(batch, padding=True, truncation=False, return_tensors="pt")
            if encoded["input_ids"].shape[1] > self.max_tokens:
                raise SemanticError("文本超过模型 token 上限；请缩短查询或减少配置中的语义窗口")
            with self.torch.inference_mode():
                vectors = self.model(**encoded).last_hidden_state[:, 0].float()
                vectors = self.torch.nn.functional.normalize(vectors, p=2, dim=1)
            output.extend(vectors.tolist())
        return output


@lru_cache(maxsize=2)
def local_embedder(config=DEFAULT_CONFIG):
    return LocalEmbedder(config)


class LazyEmbedder:
    def __init__(self, config=DEFAULT_CONFIG):
        self.config = config

    def encode(self, texts, *, query=False):
        return local_embedder(self.config).encode(texts, query=query)


def validate_vectors(vectors, count, dimension):
    if len(vectors) != count:
        raise SemanticError("向量数量不匹配")
    for vector in vectors:
        if len(vector) != dimension or any(not math.isfinite(v) for v in vector):
            raise SemanticError("向量维数错误或包含非有限数")
        norm = math.sqrt(sum(v * v for v in vector))
        if not math.isclose(norm, 1, abs_tol=1e-4):
            raise SemanticError("向量未按 L2 归一化或为零向量")


def init_cache(db):
    db.execute("""CREATE TABLE IF NOT EXISTS semantic_vectors (
        brand TEXT NOT NULL, doc_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
        config_hash TEXT NOT NULL, text_hash TEXT NOT NULL,
        config_json TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
        PRIMARY KEY(brand, doc_id, chunk_id, config_hash))""")


def _read(row, text, config):
    if not row or row[0] != digest(text) or row[1] != json.dumps(asdict(config), sort_keys=True):
        return None
    if digest(row[2]) != row[3]:
        return None
    try:
        payload = json.loads(row[2])
        parts = windows(text, config)
        if [(p["start"], p["end"]) for p in payload] != [(p["start"], p["end"]) for p in parts]:
            return None
        validate_vectors([p["vector"] for p in payload], len(parts), config.dimension)
        return payload
    except (ValueError, KeyError, TypeError):
        return None


def cached_vectors(db, chunks, embedder, *, mode="incremental"):
    """One transaction owned by the caller; failed encoding never partially writes."""
    if mode not in {"check", "incremental", "rebuild"}:
        raise ValueError("无效语义索引维护模式")
    config = embedder.config
    found, missing = {}, []
    for chunk in chunks:
        text = chunk_text(chunk)
        row = db.execute(
            "SELECT text_hash,config_json,payload,payload_hash FROM semantic_vectors "
            "WHERE brand=? AND doc_id=? AND chunk_id=? AND config_hash=?",
            (chunk.brand, chunk.doc_id, chunk.chunk_id, config.key),
        ).fetchone()
        payload = _read(row, text, config)
        if payload is None or mode == "rebuild":
            missing.append((chunk, text, windows(text, config)))
        else:
            found[chunk.chunk_id] = payload
    report = dict(
        config=asdict(config),
        config_hash=config.key,
        checked=len(chunks),
        dirty=len(missing),
        reused=len(found),
        written=0,
        windows=0,
    )
    if mode == "check":
        report["windows"] = sum(len(p) for p in found.values())
        return found, report
    flat = [p["text"] for _, _, parts in missing for p in parts]
    vectors = embedder.encode(flat) if flat else []
    validate_vectors(vectors, len(flat), config.dimension)
    offset = 0
    for chunk, text, parts in missing:
        payload = [
            dict(start=p["start"], end=p["end"], vector=vectors[offset + i])
            for i, p in enumerate(parts)
        ]
        offset += len(parts)
        raw = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        db.execute(
            "INSERT OR REPLACE INTO semantic_vectors VALUES(?,?,?,?,?,?,?,?)",
            (
                chunk.brand,
                chunk.doc_id,
                chunk.chunk_id,
                config.key,
                digest(text),
                json.dumps(asdict(config), sort_keys=True),
                raw,
                digest(raw),
            ),
        )
        found[chunk.chunk_id] = payload
    report.update(written=len(missing), windows=sum(len(p) for p in found.values()))
    return found, report


def rank_semantic(chunks, vectors, query, embedder):
    query_vector = embedder.encode([query], query=True)
    validate_vectors(query_vector, 1, embedder.config.dimension)
    q = query_vector[0]
    ranks = []
    for chunk in chunks:
        best = max(
            vectors[chunk.chunk_id],
            key=lambda p: sum(a * b for a, b in zip(q, p["vector"], strict=True)),
        )
        score = sum(a * b for a, b in zip(q, best["vector"], strict=True))
        ranks.append(
            (
                chunk.chunk_id,
                dict(
                    cosine=round(score, 7),
                    semantic_window={"start": best["start"], "end": best["end"]},
                ),
            )
        )
    return sorted(ranks, key=lambda r: (-r[1]["cosine"], r[0]))


def fuse(lexical, semantic, *, mode, pool=50):
    """RRF uses ranks, never sums incomparable BM25 and cosine magnitudes."""
    if mode not in {"semantic", "hybrid"}:
        raise ValueError("无效检索模式")
    rows = {}
    for rank, (key, info) in enumerate(semantic[:pool], 1):
        rows[key] = dict(
            info, semantic_rank=rank, score=1 / (60 + rank) if mode == "hybrid" else info["cosine"]
        )
    if mode == "hybrid":
        for rank, (key, score) in enumerate(lexical[:pool], 1):
            row = rows.setdefault(key, {"score": 0})
            row.update(bm25_score=score, lexical_rank=rank)
            row["score"] += 1 / (60 + rank)
    return rows
