from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BlockKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    BLOCK_KIND_UNSPECIFIED: _ClassVar[BlockKind]
    BLOCK_KIND_TEXT: _ClassVar[BlockKind]
    BLOCK_KIND_TABLE_CELL: _ClassVar[BlockKind]
    BLOCK_KIND_TABLE_ROW: _ClassVar[BlockKind]

class ExtractionStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EXTRACTION_STATUS_UNSPECIFIED: _ClassVar[ExtractionStatus]
    EXTRACTION_STATUS_OK: _ClassVar[ExtractionStatus]
    EXTRACTION_STATUS_INCOMPLETE: _ClassVar[ExtractionStatus]
    EXTRACTION_STATUS_REJECTED: _ClassVar[ExtractionStatus]
BLOCK_KIND_UNSPECIFIED: BlockKind
BLOCK_KIND_TEXT: BlockKind
BLOCK_KIND_TABLE_CELL: BlockKind
BLOCK_KIND_TABLE_ROW: BlockKind
EXTRACTION_STATUS_UNSPECIFIED: ExtractionStatus
EXTRACTION_STATUS_OK: ExtractionStatus
EXTRACTION_STATUS_INCOMPLETE: ExtractionStatus
EXTRACTION_STATUS_REJECTED: ExtractionStatus

class BoundingBox(_message.Message):
    __slots__ = ("x0", "y0", "x1", "y1")
    X0_FIELD_NUMBER: _ClassVar[int]
    Y0_FIELD_NUMBER: _ClassVar[int]
    X1_FIELD_NUMBER: _ClassVar[int]
    Y1_FIELD_NUMBER: _ClassVar[int]
    x0: float
    y0: float
    x1: float
    y1: float
    def __init__(self, x0: _Optional[float] = ..., y0: _Optional[float] = ..., x1: _Optional[float] = ..., y1: _Optional[float] = ...) -> None: ...

class Block(_message.Message):
    __slots__ = ("block_id", "page", "text", "bbox", "kind", "ocr_confidence")
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    BBOX_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    OCR_CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    block_id: str
    page: int
    text: str
    bbox: BoundingBox
    kind: BlockKind
    ocr_confidence: float
    def __init__(self, block_id: _Optional[str] = ..., page: _Optional[int] = ..., text: _Optional[str] = ..., bbox: _Optional[_Union[BoundingBox, _Mapping]] = ..., kind: _Optional[_Union[BlockKind, str]] = ..., ocr_confidence: _Optional[float] = ...) -> None: ...

class ExtractRequirementsRequest(_message.Message):
    __slots__ = ("job_id", "job_doc_id", "blocks", "template_set_version")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_DOC_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCKS_FIELD_NUMBER: _ClassVar[int]
    TEMPLATE_SET_VERSION_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    job_doc_id: str
    blocks: _containers.RepeatedCompositeFieldContainer[Block]
    template_set_version: str
    def __init__(self, job_id: _Optional[str] = ..., job_doc_id: _Optional[str] = ..., blocks: _Optional[_Iterable[_Union[Block, _Mapping]]] = ..., template_set_version: _Optional[str] = ...) -> None: ...

class Requirement(_message.Message):
    __slots__ = ("template_id", "text", "kind", "mandatory", "value", "unit", "source_block_id", "page", "bbox", "confidence", "needs_review")
    TEMPLATE_ID_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    MANDATORY_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    BBOX_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    NEEDS_REVIEW_FIELD_NUMBER: _ClassVar[int]
    template_id: str
    text: str
    kind: str
    mandatory: bool
    value: str
    unit: str
    source_block_id: str
    page: int
    bbox: BoundingBox
    confidence: float
    needs_review: bool
    def __init__(self, template_id: _Optional[str] = ..., text: _Optional[str] = ..., kind: _Optional[str] = ..., mandatory: _Optional[bool] = ..., value: _Optional[str] = ..., unit: _Optional[str] = ..., source_block_id: _Optional[str] = ..., page: _Optional[int] = ..., bbox: _Optional[_Union[BoundingBox, _Mapping]] = ..., confidence: _Optional[float] = ..., needs_review: _Optional[bool] = ...) -> None: ...

class NumberValue(_message.Message):
    __slots__ = ("value", "unit", "kind", "condition", "role", "source_block_id", "page", "bbox", "confidence")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    BBOX_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    value: str
    unit: str
    kind: str
    condition: str
    role: str
    source_block_id: str
    page: int
    bbox: BoundingBox
    confidence: float
    def __init__(self, value: _Optional[str] = ..., unit: _Optional[str] = ..., kind: _Optional[str] = ..., condition: _Optional[str] = ..., role: _Optional[str] = ..., source_block_id: _Optional[str] = ..., page: _Optional[int] = ..., bbox: _Optional[_Union[BoundingBox, _Mapping]] = ..., confidence: _Optional[float] = ...) -> None: ...

class ExtractRequirementsResponse(_message.Message):
    __slots__ = ("job_id", "requirements", "numbers", "status", "status_reason")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    REQUIREMENTS_FIELD_NUMBER: _ClassVar[int]
    NUMBERS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STATUS_REASON_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    requirements: _containers.RepeatedCompositeFieldContainer[Requirement]
    numbers: _containers.RepeatedCompositeFieldContainer[NumberValue]
    status: ExtractionStatus
    status_reason: str
    def __init__(self, job_id: _Optional[str] = ..., requirements: _Optional[_Iterable[_Union[Requirement, _Mapping]]] = ..., numbers: _Optional[_Iterable[_Union[NumberValue, _Mapping]]] = ..., status: _Optional[_Union[ExtractionStatus, str]] = ..., status_reason: _Optional[str] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthResponse(_message.Message):
    __slots__ = ("ready",)
    READY_FIELD_NUMBER: _ClassVar[int]
    ready: bool
    def __init__(self, ready: _Optional[bool] = ...) -> None: ...
