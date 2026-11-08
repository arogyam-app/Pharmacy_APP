"""Shared schema building blocks."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """Response models read straight off SQLAlchemy rows."""

    model_config = ConfigDict(from_attributes=True)


class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Page(BaseModel, Generic[T]):
    """The one list-response shape used by every collection endpoint."""

    items: list[T]
    total: int
    page: int
    page_size: int

    @classmethod
    def build(cls, items: list[T], total: int, params: PageParams) -> "Page[T]":
        return cls(items=items, total=total, page=params.page, page_size=params.page_size)


class Message(BaseModel):
    message: str
