"""Literal, versioned query terms. These rules never rewrite source evidence."""

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

from autorepair.search import tokenize

PATH = Path(__file__).resolve().parents[2] / "configs/qa/query-terms.yaml"


@lru_cache(maxsize=1)
def rules():
    data = yaml.safe_load(PATH.read_text())
    if set(data) != {"version", "aliases", "stop_phrases"}:
        raise ValueError("问句术语配置字段无效")
    if not isinstance(data["version"], str) or not data["version"].strip():
        raise ValueError("问句术语配置缺少版本")
    aliases = data["aliases"]
    if not isinstance(aliases, dict) or any(
        not isinstance(k, str) or not k or not isinstance(v, str) or not v
        for k, v in aliases.items()
    ):
        raise ValueError("问句别名须为非空字符串映射")
    if not isinstance(data["stop_phrases"], list) or any(
        not isinstance(s, str) or not s for s in data["stop_phrases"]
    ):
        raise ValueError("问句停用语无效")
    return data


def normalize(question):
    data = rules()
    aliases = data["aliases"]
    changes = []

    def replace(match):
        source = match.group()
        changes.append({"source": source, "canonical": aliases[source]})
        return aliases[source]

    pattern = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
    query = re.sub(pattern, replace, question)
    return query, dict(
        version=data["version"],
        config_hash=hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        replacements=changes,
        retrieval_query=query,
    )


def topic(question, intent_patterns):
    for pattern in intent_patterns:
        question = re.sub(pattern, " ", question)
    stop = "|".join(re.escape(s) for s in sorted(rules()["stop_phrases"], key=len, reverse=True))
    return set(tokenize(re.sub(stop, " ", question)))


def relevant(question, text, intent_patterns):
    tokens = topic(question, intent_patterns)
    overlap = tokens & set(tokenize(text))
    return bool(tokens and overlap and len(overlap) / len(tokens) >= 0.5)
