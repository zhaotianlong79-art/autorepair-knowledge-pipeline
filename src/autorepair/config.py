"""Strict YAML configuration with explicit override and alias conflict validation."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from autorepair.models import Model

Action = Literal[
    "structure",
    "terms",
    "references",
    "tools",
    "components",
    "style",
    "long_tail",
    "pii",
    "retrieval",
]
ORDER = [
    "structure",
    "terms",
    "references",
    "tools",
    "components",
    "style",
    "long_tail",
    "pii",
    "retrieval",
]


class Rule(Model):
    id: str
    version: str = "1"
    action: Action
    overrides: bool = False


class Term(Model):
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["term", "tool", "component", "long_tail"] = "term"
    entity_id: str


class Config(Model):
    brand: str
    rules: list[Rule]
    terms: list[Term] = Field(default_factory=list)
    protected_terms: list[str] = Field(default_factory=list)
    known_terms: list[str] = Field(default_factory=list)
    declared_documents: list[str] = Field(default_factory=list)

    @property
    def digest(self):
        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key, value in node.value:
        key = loader.construct_object(key)
        if key in result:
            raise ValueError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_config(directory: Path, brand: str) -> Config:
    # Brands are identifiers, not filesystem paths.
    if not brand or not all(c.isalnum() or c in "_-" for c in brand):
        raise ValueError("Invalid brand identifier")
    common = yaml.load((directory / "common.yaml").read_text(), Loader=UniqueLoader)
    specific = yaml.load((directory / "brands" / f"{brand}.yaml").read_text(), Loader=UniqueLoader)
    if specific.get("brand") != brand:
        raise ValueError("Brand configuration mismatch")
    rules = {}
    for source in [common, specific]:
        seen = set()
        for data in source.get("rules", []):
            rule = Rule.model_validate(data)
            if rule.id in seen:
                raise ValueError(f"Duplicate rule ID: {rule.id}")
            seen.add(rule.id)
            if (rule.id in rules) != rule.overrides:
                raise ValueError(
                    f"Rule {rule.id}: override must explicitly replace an existing rule"
                )
            rules[rule.id] = rule
    actions = [r.action for r in rules.values()]
    if len(actions) != len(set(actions)):
        raise ValueError("Only one rule per processing stage is allowed")
    merged = dict(common)
    merged.update(specific)
    for key in ["terms", "protected_terms", "known_terms", "declared_documents"]:
        merged[key] = common.get(key, []) + specific.get(key, [])
    merged["rules"] = sorted(rules.values(), key=lambda r: ORDER.index(r.action))
    cfg = Config.model_validate(merged)
    aliases = {}
    entities = set()
    for term in cfg.terms:
        if term.entity_id in entities:
            raise ValueError(f"Duplicate entity: {term.entity_id}")
        entities.add(term.entity_id)
        for alias in [term.canonical, *term.aliases]:
            if not alias.strip():
                raise ValueError("Empty term alias")
            key = alias.casefold()
            if key in aliases and aliases[key] != term.canonical:
                raise ValueError(f"Ambiguous term alias: {alias}")
            aliases[key] = term.canonical
    for alias, canonical in aliases.items():
        if canonical.casefold() in aliases and aliases[canonical.casefold()] != canonical:
            raise ValueError(f"Cyclic term mapping: {alias}")
    for protected in cfg.protected_terms:
        if protected.casefold() in aliases and aliases[protected.casefold()] != protected:
            raise ValueError(f"Protected term is a replacement alias: {protected}")
    return cfg
