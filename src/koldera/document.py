"""The structured document of ``POST /v1/documents/jobs`` (``contracts/document.schema.json``).

Structure is declared, never inferred. Images are referenced by the ``uploadId`` of an earlier image upload.
Documents are plain dictionaries on the wire; these ``TypedDict`` definitions let a type checker verify them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, TypedDict


class DocumentLink(TypedDict):
    text: str
    url: str


class StrongInline(TypedDict):
    strong: str


class EmphasisInline(TypedDict):
    emphasis: str


class LinkInline(TypedDict):
    link: DocumentLink


class _ListBodyRequired(TypedDict):
    items: Sequence[Sequence[Inline | NestedList]]


class ListBody(_ListBodyRequired, total=False):
    ordered: bool


class NestedList(TypedDict):
    list: ListBody


#: Running text: a plain string, strong text, emphasised text or a link.
Inline = str | StrongInline | EmphasisInline | LinkInline
#: Inline pieces, optionally followed by one nested list as the last element.
ListItem = Sequence[Inline | NestedList]


class HeadingBlock(TypedDict):
    type: Literal["heading"]
    level: Literal[1, 2, 3, 4, 5, 6]
    text: str


class _ParagraphRequired(TypedDict):
    type: Literal["paragraph"]
    content: Sequence[Inline]


class ParagraphBlock(_ParagraphRequired, total=False):
    #: BCP 47 tag when it differs from the document language.
    lang: str
    #: A bundled font name when it differs from the theme font.
    font: str


class _ListBlockRequired(TypedDict):
    type: Literal["list"]
    items: Sequence[ListItem]


class ListBlock(_ListBlockRequired, total=False):
    ordered: bool


class _TableRequired(TypedDict):
    type: Literal["table"]
    caption: str
    #: At least one row with a non-blank cell; the first row sets the column count.
    header: Sequence[Sequence[str]]
    rows: Sequence[Sequence[str]]


class TableBlock(_TableRequired, total=False):
    rowHeaders: bool
    columnAlign: Sequence[Literal["left", "center", "right"]]


class DescribedImageBlock(TypedDict):
    type: Literal["image"]
    uploadId: str
    alt: str


class DecorativeImageBlock(TypedDict):
    type: Literal["image"]
    uploadId: str
    decorative: Literal[True]


#: An image uploaded earlier (``documents.upload_image``), described or marked decorative.
ImageBlock = DescribedImageBlock | DecorativeImageBlock


class LinkBlock(TypedDict):
    type: Literal["link"]
    text: str
    url: str


class PageBreakBlock(TypedDict):
    type: Literal["pageBreak"]


class SpacerBlock(TypedDict):
    type: Literal["spacer"]
    #: A length with a unit: mm, cm, in or pt, for example ``8mm``.
    height: str


class KeyValueBlock(TypedDict):
    type: Literal["keyValue"]
    #: ``[label, value]`` pairs.
    rows: Sequence[Sequence[str]]


class PageNumberBlock(TypedDict):
    type: Literal["pageNumber"]


Block = HeadingBlock | ParagraphBlock | ListBlock | TableBlock | ImageBlock | LinkBlock | PageBreakBlock | SpacerBlock | KeyValueBlock
FooterBlock = Block | PageNumberBlock


class DocumentPage(TypedDict, total=False):
    size: Literal["A4", "A5", "Letter", "Legal"]
    orientation: Literal["portrait", "landscape"]
    #: A length with a unit, for example ``20mm``.
    margins: str


class DocumentTheme(TypedDict, total=False):
    font: str
    #: Points, 6-24.
    fontSize: int
    #: ``#rrggbb``.
    accent: str


class _DocumentRequired(TypedDict):
    title: str
    #: BCP 47 tag, for example ``en`` or ``uk``.
    language: str
    body: Sequence[Block]


class StructuredDocument(_DocumentRequired, total=False):
    page: DocumentPage
    theme: DocumentTheme
    header: Sequence[Block]
    footer: Sequence[FooterBlock]
