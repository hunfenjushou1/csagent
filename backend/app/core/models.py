"""核心数据契约：模板字段、语料块、填充结果。

设计原则：所有「能给用户看的结论」都必须挂证据（evidence），
凡是没证据的字段一律落到 missing 状态并向用户追问，不允许模型凭空生成。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class FieldStatus(str, Enum):
    """字段三态。filled 必须有证据链，否则只能是另外两种。"""

    FILLED = "filled"              # 已按证据填充并校验通过
    PENDING = "pending_confirm"    # 检索到候选依据，等待人工/模型确认
    MISSING = "missing"            # 完全无依据，必须向用户追问


class FieldKind(str, Enum):
    TABLE_CELL = "table_cell"
    PARAGRAPH = "paragraph"
    PLACEHOLDER = "placeholder"


class ValueType(str, Enum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    DATE = "date"
    LIST = "list"
    NUMBER = "number"


@dataclass
class TemplateField:
    """模板中一个待填位置。field_id 在一次任务内稳定，便于前端回跳。"""

    field_id: str
    kind: FieldKind
    label: str                      # 列名 / 段首标签
    hint: str                       # 给检索和模型看的上下文
    section: str = ""               # 所属章节标题
    caption: str = ""               # 所在表格标题
    value_type: ValueType = ValueType.TEXT
    current_value: str = ""

    # 写回定位（docx）
    table_index: int | None = None
    row_index: int | None = None
    col_index: int | None = None
    paragraph_index: int | None = None

    # 占位符模式专用：占位符名即语义标签，同名占位符全局同值
    placeholder: str = ""
    occurrence: int = 1

    # 结果
    status: FieldStatus = FieldStatus.MISSING
    value: str = ""
    evidence: list[str] = field(default_factory=list)   # chunk_id 列表
    question: str = ""                                  # missing 时给用户的追问
    confidence: float = 0.0                             # 依据强度 0-1，低于阈值一律按缺失处理
    basis: str = ""                                     # 依据摘要，给用户看的判定理由

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["value_type"] = self.value_type.value
        d["status"] = self.status.value
        return d


@dataclass
class Chunk:
    """参考文件切出的语料块，locator 必须能指回原文，方便用户核验。"""

    chunk_id: str
    doc_name: str
    locator: str          # 第 3 页 / 第 12 段 / Sheet1!B4
    text: str
    section: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TemplateSpec:
    """一份模板的解析结果，含内容指纹用于版本锁定。"""

    template_id: str
    name: str
    source: str                     # local / gitlab
    source_ref: str = ""            # commit sha 或文件哈希
    fields: list[TemplateField] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.template_id:
            self.template_id = uuid.uuid4().hex[:12]

    @property
    def blank_fields(self) -> list[TemplateField]:
        return [f for f in self.fields if not f.current_value.strip()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "source": self.source,
            "source_ref": self.source_ref,
            "field_count": len(self.fields),
            "blank_count": len(self.blank_fields),
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass
class ProcessingReport:
    """一次处理任务的汇总，直接喂给前端审阅页。"""

    task_id: str
    template_name: str
    fields: list[TemplateField]
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def stats(self) -> dict[str, int]:
        out = {s.value: 0 for s in FieldStatus}
        for f in self.fields:
            out[f.status.value] += 1
        out["total"] = len(self.fields)
        return out

    @property
    def questions(self) -> list[dict[str, str]]:
        """只把真正缺失的字段变成追问，避免用泛泛的「信息不足」打扰用户。"""
        return [
            {"field_id": f.field_id, "label": f.label, "question": f.question}
            for f in self.fields
            if f.status == FieldStatus.MISSING
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "template_name": self.template_name,
            "stats": self.stats,
            "questions": self.questions,
            "fields": [f.to_dict() for f in self.fields],
            "chunks": [c.to_dict() for c in self.chunks],
        }


def file_fingerprint(path: str) -> str:
    """模板内容指纹，用于确认「同一份模板」生成结果可复现。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()[:16]
