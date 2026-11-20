"""Stable contracts shared by parsers, rules, storage and review UI."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Block(Model):
    block_id: str
    kind: str = "paragraph"
    original: str
    text: str
    location: dict = Field(default_factory=dict)
    section: str = ""
    links: list[str] = Field(default_factory=list)


class Issue(Model):
    code: str
    severity: Literal["warning", "error"]
    message: str
    block_id: str = ""


class Change(Model):
    rule_id: str
    rule_version: str
    block_id: str
    before: str
    after: str


class Document(Model):
    schema_version: str = "1.0"
    doc_id: str
    brand: str
    model: str = "unknown"
    year_range: str = "unknown"
    source_name: str
    source_hash: str
    parser: str
    blocks: list[Block] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    expected_blocks: int | None = None
    component_mentions: list[dict] = Field(default_factory=list)


class Result(Model):
    document: Document
    rules_hash: str
    changes: list[Change] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    status: Literal["passed", "review", "failed"] = "passed"
    reviews: list[dict] = Field(default_factory=list)

    def finalize(self):
        self.status = (
            "failed"
            if any(i.severity == "error" for i in self.issues)
            else "review"
            if self.issues
            else "passed"
        )
        return self
