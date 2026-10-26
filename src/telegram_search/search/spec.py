"""Versioned search intent shared by jobs, saved searches and user interfaces."""

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from telegram_search.search.lexical import Filters, date_bound
from telegram_search.shared.text import serialize

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
ForwardSource = Annotated[str, Field(min_length=1, max_length=4096)]


class SearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["all", "selected"] = "all"
    chat_ids: list[Identifier] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.mode == "all" and self.chat_ids:
            raise ValueError("An all-chat scope cannot also select individual chats")
        return self


class SearchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    query: str = Field(default="", max_length=4096)
    scope: SearchScope = Field(default_factory=SearchScope)
    author_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    forwarded_from: list[ForwardSource] = Field(default_factory=list, max_length=500)
    date_from: str | None = Field(default=None, max_length=64)
    date_to: str | None = Field(default=None, max_length=64)
    content_type: Literal["all", "text", "photo", "service"] = "all"
    exclude_deleted: bool = Field(default=False, strict=True)
    exact_phrase: bool = Field(default=False, strict=True)
    mode: Literal["words", "meaning", "hybrid"] = "hybrid"
    modalities: list[Literal["text", "images", "ocr"]] = Field(
        default_factory=lambda: ["text", "images", "ocr"], min_length=1, max_length=3
    )
    strategy: Literal["exact", "ann"] = "exact"
    sort: Literal["newest", "oldest"] = "newest"
    page_size: int = Field(default=20, ge=1, le=100, strict=True)
    chunk_size: int = Field(default=10, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def validate_dates(self):
        lower, upper = date_bound(self.date_from), date_bound(self.date_to, end=True)
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("The start of the period must precede its end")
        return self

    @property
    def browsing(self):
        return not self.query.strip()

    def filters(self):
        return Filters(
            chat_ids=list(self.scope.chat_ids),
            author_ids=list(self.author_ids),
            date_from=date_bound(self.date_from),
            date_to=date_bound(self.date_to, end=True),
            content_type=self.content_type,
            exclude_deleted=self.exclude_deleted,
            forwarded_from=list(self.forwarded_from),
            scope_mode=self.scope.mode,
        )

    def canonical(self):
        value = self.model_dump(mode="json")
        value["scope"]["chat_ids"] = sorted(set(value["scope"]["chat_ids"]))
        for name in ("author_ids", "forwarded_from", "modalities"):
            value[name] = sorted(set(value[name]))
        # Whitespace inside a quoted phrase remains significant.
        value["query"] = value["query"].strip()
        return value

    @property
    def identity(self):
        return hashlib.sha256(serialize(self.canonical()).encode()).hexdigest()
