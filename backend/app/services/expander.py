"""集合占位符的动态扩行。

问题：模板里只有 `{活动1名称}` ~ `{活动4名称}` 四行，而接口协议 excel 有 5 条活动。
第 5 条没有位置可放——这是占位符模板的固有缺陷：模板把行数写死了。

分两步解决，缺一不可：
1. **字段层**（本模块）：按数据条数补齐 / 裁掉集合字段，让每个数据行都有占位符；
2. **文档层**（exporter）：导出时复制 / 删除表格行，让新占位符真的出现在文档里。
只做 1 会多出无处落地的字段；只做 2 则新行没有值可填。

取舍：
- 数据条数拿不到时（没上传对应的表），**保持模板原样**，不猜、不缩；
- 数据源空表也不动，缩到 1 行反而会丢掉模板本来要用户手填的位置；
- 行数上限 MAX_ROWS，防止一份 200 行的 excel 把文档撑爆。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from ..core.models import FieldKind, TemplateField
from ..parsers.docx_template import infer_value_type
from .dataset import SEQ_RE, DataTable, SequenceResolver

MAX_ROWS = 50


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """字段既可能是 TemplateField，也可能是 to_dict 后的 dict，统一取值。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_placeholder(obj: Any) -> bool:
    kind = _attr(obj, "kind", "")
    return str(getattr(kind, "value", kind)) == FieldKind.PLACEHOLDER.value


@dataclass
class Collection:
    """模板里一个「一行一条数据」的集合，如活动表、成员表。"""

    name: str                                  # 活动 / 成员
    table_index: int
    seq_row: dict[int, int] = dc_field(default_factory=dict)      # 序号 → 模板行号
    seq_attrs: dict[int, set[str]] = dc_field(default_factory=dict)  # 序号 → 该行占位符属性
    attr_col: dict[str, int] = dc_field(default_factory=dict)     # 属性 → 模板列号

    @property
    def max_seq(self) -> int:
        return max(self.seq_row) if self.seq_row else 0

    @property
    def min_seq(self) -> int:
        return min(self.seq_row) if self.seq_row else 0

    def rich_seq(self) -> int:
        """占位符最全的那一行 —— 扩行时复制它，才不会丢列。

        模板里偶尔会出现某行漏写占位符的情况，挑行号最大的那行来复制有风险。
        """
        if not self.seq_attrs:
            return self.max_seq
        return max(self.seq_attrs, key=lambda s: (len(self.seq_attrs[s]), s))

    def rich_attrs(self) -> list[str]:
        """按模板列顺序返回属性，而不是字母序。

        否则前端看到的「活动5」字段会是 交付物 / 名称 / 开始时间 / 编号……
        跟模板里 编号-名称-责任人-开始时间 的顺序对不上，审阅时容易看漏。
        """
        attrs = self.seq_attrs.get(self.rich_seq(), set())
        return sorted(attrs, key=lambda a: (self.attr_col.get(a, 999), a))

    @classmethod
    def from_fields(cls, fields: list[Any]) -> dict[tuple[int, str], "Collection"]:
        groups: dict[tuple[int, str], Collection] = {}
        for f in fields:
            if not _is_placeholder(f):
                continue
            placeholder = (_attr(f, "placeholder") or "").strip()
            table_index = _attr(f, "table_index")
            row_index = _attr(f, "row_index")
            col_index = _attr(f, "col_index")
            if not placeholder or table_index is None:
                continue
            match = SEQ_RE.match(placeholder)
            if not match:
                continue
            name, seq, attr = match.group(1), int(match.group(2)), match.group(3)
            coll = groups.setdefault(
                (table_index, name), Collection(name=name, table_index=table_index)
            )
            if row_index is not None:
                coll.seq_row[seq] = row_index
            coll.seq_attrs.setdefault(seq, set()).add(attr)
            if col_index is not None:
                coll.attr_col.setdefault(attr, col_index)
        return groups


@dataclass
class CollectionChange:
    collection: Collection
    source: str              # 数据源描述，给用户看
    target_rows: int
    added: list[int] = dc_field(default_factory=list)
    removed: list[int] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection.name,
            "source": self.source,
            "template_rows": self.collection.max_seq,
            "target_rows": self.target_rows,
            "added": self.added,
            "removed": self.removed,
        }


class ExpansionPlanner:
    """决定每个集合该有几行，并把字段清单改成对应的形状。"""

    def __init__(self, tables: list[DataTable], max_rows: int = MAX_ROWS) -> None:
        self.resolver = SequenceResolver(tables)
        self.max_rows = max_rows

    def plan(self, fields: list[Any]) -> list[CollectionChange]:
        changes: list[CollectionChange] = []
        for coll in Collection.from_fields(fields).values():
            count, source = self._data_count(coll)
            if count is None:
                continue                        # 找不到数据源：保持模板原样
            target = max(1, min(count, self.max_rows))
            if target == coll.max_seq:
                continue
            changes.append(
                CollectionChange(
                    collection=coll,
                    source=source,
                    target_rows=target,
                    added=[s for s in range(coll.max_seq + 1, target + 1)],
                    removed=[s for s in range(target + 1, coll.max_seq + 1)],
                )
            )
        return changes

    def _data_count(self, coll: Collection) -> tuple[int | None, str]:
        table = self.resolver.table_for(coll.name)
        if table is None:
            return None, ""
        # 表名带「活动」但表头对不上任何属性 —— 不是这个集合的数据表，别乱用
        if not any(table.col_index(a) is not None for a in coll.attr_col):
            return None, ""
        rows = table.data_rows()
        if not rows:
            return None, ""
        return len(rows), f"《{table.source}》「{table.name}」{len(rows)} 条"

    # ---------- 把计划落到字段清单上 ----------

    def apply(self, fields: list[Any], changes: list[CollectionChange]) -> list[Any]:
        """返回新的字段列表：裁掉超出的行，补齐缺的行，并重排 field_id。

        field_id 必须重排——扩行后如果新增字段沿用旧编号会撞号，前端回跳就乱了。
        重排是稳定的：同一份模板 + 同一批资料，每次算出的顺序一致。
        """
        if not changes:
            return fields

        removed_keys = {
            (c.collection.table_index, c.collection.name, seq)
            for c in changes
            for seq in c.removed
        }

        def _key(f: Any) -> tuple[int, str, int] | None:
            if not _is_placeholder(f):
                return None
            placeholder = (_attr(f, "placeholder") or "").strip()
            table_index = _attr(f, "table_index")
            if not placeholder or table_index is None:
                return None
            match = SEQ_RE.match(placeholder)
            if not match:
                return None
            return table_index, match.group(1), int(match.group(2))

        kept = [f for f in fields if _key(f) not in removed_keys]

        # 新增字段挂在「该集合最后一个字段」之后，保证表格区域连续
        last_pos: dict[tuple[int, str], int] = {}
        for i, f in enumerate(kept):
            key = _key(f)
            if key:
                last_pos[(key[0], key[1])] = i

        pending: dict[tuple[int, str], list[TemplateField]] = {}
        for change in changes:
            coll = change.collection
            if not change.added:
                continue
            attrs = coll.rich_attrs()
            anchor_row = coll.seq_row.get(coll.rich_seq())
            rows: list[TemplateField] = []
            for seq in change.added:
                rows.extend(self._make_fields(coll, seq, attrs, anchor_row))
            pending[(coll.table_index, coll.name)] = rows

        out: list[Any] = []
        for i, f in enumerate(kept):
            out.append(f)
            key = _key(f)
            if key and last_pos.get((key[0], key[1])) == i:
                out.extend(pending.get((key[0], key[1]), []))

        for i, f in enumerate(out, start=1):
            if isinstance(f, dict):
                f["field_id"] = f"F{i:03d}"
            else:
                f.field_id = f"F{i:03d}"
        return out

    @staticmethod
    def _make_fields(
        coll: Collection, seq: int, attrs: list[str], anchor_row: int | None
    ) -> list[TemplateField]:
        out: list[TemplateField] = []
        for attr in attrs:
            name = f"{coll.name}{seq}{attr}"
            out.append(
                TemplateField(
                    field_id="",
                    kind=FieldKind.PLACEHOLDER,
                    label=name,
                    hint=(
                        f"占位符「{name}」，由数据源第 {seq} 行按条数动态扩展生成"
                        f"（模板原只有 {coll.max_seq} 行）"
                    ),
                    placeholder=name,
                    value_type=infer_value_type(attr),
                    table_index=coll.table_index,
                    row_index=anchor_row,
                    col_index=coll.attr_col.get(attr),
                )
            )
        return out


def expand_fields(
    fields: list[Any], tables: list[DataTable], max_rows: int = MAX_ROWS
) -> tuple[list[Any], list[CollectionChange]]:
    """一步到位：算计划 + 改字段。没数据源时原样返回。"""
    planner = ExpansionPlanner(tables, max_rows=max_rows)
    changes = planner.plan(fields)
    return planner.apply(fields, changes), changes
