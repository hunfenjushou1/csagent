"""语义对齐：让模型在「字段」和「资料原文」之间建立对应关系。

为什么规则不够：模板写「活动名称」、资料写「阶段任务」，模板写「交付物」、
excel 写 `Work Product`——字面不匹配，规则只能判缺失。而这恰恰是用户明知
资料里有、却被系统反问的场景。

模型在这里只做一件事：指出「哪一段原文里的哪几个字，对应这个字段」。
**它的输出不直接进正文**，必须先过 verify()：
  1. 引用的资料块必须真实存在
  2. 引用的原文必须逐字出现在该块里（防编造引文）
  3. 取值必须能在该块原文中找到对应（防编造内容）
三条任一不过，就当作"模型也没找到"，照旧向用户追问，并在追问里带上模型说明。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ..core.models import Chunk, FieldStatus, TemplateField, ValueType
from .dataset import DataTable, SequenceResolver
from .llm import ResultCache
from .questions import build_question

TYPE_CN = {
    ValueType.TEXT: "短文本",
    ValueType.LONG_TEXT: "段落/长文本",
    ValueType.DATE: "日期",
    ValueType.LIST: "清单/多值",
    ValueType.NUMBER: "数值",
}

SYSTEM_PROMPT = """你是企业文档填写助手。任务：从【参考资料】原文中，为每个【待填字段】找到直接依据，并给出该字段的取值。

铁律（违反即为错误输出）：
1. 只能使用【参考资料】里逐字出现的信息。禁止使用任何常识、行业惯例、模型记忆或外部知识补全。
2. 只要给了 value，就必须同时给出 evidence_chunk（资料块编号）和 quote（从该块逐字复制的原文，10~120 字，不得改写、不得拼接、不得跨块）。
3. 找不到依据时 value 必须为 null，并在 reason 里说明：资料里实际有什么、缺的是什么。不得猜测、不得举例、不得用"通常/一般/建议"之类的话编内容。
4. 语义相近但对象不同，一律判为找不到。例：字段要「网络安全接口人」，资料写的是「网络安全接口协议」，这是两个东西，必须判 null。
5. 允许的加工：日期格式换算、把长句压缩成字段所需的短语、从表格行中取出某一列的单元格值。不允许：改动数字、把"计划"写成"已完成"、把多人合并成另一个人的姓名。
6. 同一字段有多条依据时，选最直接、最完整的一条；字段是清单型时，可以把同一份资料中的多个条目按原文顺序合并。

只输出 JSON，不要任何解释文字：
{"results":[{"field_id":"F001","value":"取值或 null","evidence_chunk":"c12 或 null","quote":"原文逐字片段或 null","confidence":0.0,"reason":"一句话依据；找不到时说明缺什么"}]}"""

ATTR_SYSTEM = """你是表格字段映射助手。给定「模板需要的属性名」和「数据表的列名」，判断每个属性应对应哪一列。

规则：
1. 只依据语义对应判断，不做猜测。用词不同但指同一事物算对应（如「交付物」对「Work Product」）。
2. 概念不同不算对应（如「开始时间」对「Target Date」是预计完成时间，不是开始时间，必须判 null）。
3. 没有合适的列就返回 null，不要硬凑。
4. column 必须从给定的列名中原样复制。

只输出 JSON：{"mapping":[{"attr":"交付物","column":"Work Product"}]}"""


def _norm(text: str) -> str:
    """去掉空白和标点后比较，避免「2026-05-30」和「2026 年 5 月 30 日」被判不同。"""
    return re.sub(r"[\W_]+", "", text or "").lower()


DATE_RE = re.compile(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})")
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
LIST_SPLIT = re.compile(r"[、,，;；/|\n]+|和|及|与")


def _dates(text: str) -> set[tuple[int, int, int]]:
    out: set[tuple[int, int, int]] = set()
    for y, m, d in DATE_RE.findall(text or ""):
        try:
            out.add((int(y), int(m), int(d)))
        except ValueError:
            continue
    return out


def _ngram_ratio(value: str, text: str, n: int = 4) -> float:
    a, b = _norm(value), _norm(text)
    if not a:
        return 0.0
    if len(a) < n:
        return 1.0 if a in b else 0.0
    grams = [a[i : i + n] for i in range(len(a) - n + 1)]
    hit = sum(1 for g in grams if g in b)
    return hit / len(grams)


def is_traceable(value: str, text: str, value_type: ValueType) -> bool:
    """取值能否在这段原文里找到出处。

    这是防幻觉的最后一道闸：模型可以改写措辞，但不能凭空造出一个
    资料里根本没有的姓名、日期或金额。
    """
    if not value.strip():
        return False
    nv, nt = _norm(value), _norm(text)
    if nv and nv in nt:
        return True

    if value_type == ValueType.DATE:
        return bool(_dates(value) & _dates(text))
    if value_type == ValueType.NUMBER:
        nums = NUM_RE.findall(value)
        pool = set(NUM_RE.findall(text))
        return bool(nums) and all(n in pool for n in nums)
    if value_type == ValueType.LIST:
        items = [i for i in LIST_SPLIT.split(value) if len(_norm(i)) >= 2]
        if not items:
            return False
        hit = sum(1 for i in items if _norm(i) in nt)
        if len(items) <= 3:
            # 短清单必须逐项有出处：两条里编一条，等于整条清单不可信
            return hit == len(items)
        return hit >= max(1, int(len(items) * 0.6))
    if value_type == ValueType.LONG_TEXT:
        # 长段落允许模型压缩改写，但内容主体必须来自原文
        return _ngram_ratio(value, text) >= 0.3
    return _ngram_ratio(value, text) >= 0.75


@dataclass
class AlignStats:
    enabled: bool = False
    model: str = ""
    attempted: int = 0        # 进入对齐的缺失字段数
    accepted: int = 0         # 通过校验、写入候选值
    rejected: int = 0         # 模型给了值但校验不过
    empty: int = 0            # 模型也明确找不到
    cached: int = 0           # 命中缓存、未产生调用
    calls: int = 0
    tokens: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "attempted": self.attempted,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "empty": self.empty,
            "cached": self.cached,
            "calls": self.calls,
            "tokens": self.tokens,
            "note": self.note,
        }


@dataclass
class FieldResult:
    accepted: bool
    value: str = ""
    chunk_id: str = ""
    quote: str = ""
    confidence: float = 0.0
    reason: str = ""


class SemanticAligner:
    def __init__(
        self,
        client: Any | None,
        cache: ResultCache | None = None,
        max_chars_per_call: int = 12000,
        chunk_char_limit: int = 900,
        max_batches: int = 8,
    ) -> None:
        self.client = client
        self.cache = cache
        self.max_chars_per_call = max_chars_per_call
        self.chunk_char_limit = chunk_char_limit
        self.max_batches = max_batches

    @property
    def enabled(self) -> bool:
        return bool(self.client and getattr(self.client, "enabled", False))

    @property
    def model(self) -> str:
        return getattr(getattr(self.client, "settings", None), "model", "") or ""

    # ---------- 对外入口 ----------

    def align(
        self,
        fields: list[TemplateField],
        chunks: list[Chunk],
        question: str = "",
    ) -> AlignStats:
        stats = AlignStats(enabled=self.enabled, model=self.model)
        if not self.enabled:
            stats.note = "未配置 DEEPSEEK_API_KEY，仅规则模式"
            return stats

        targets = [f for f in fields if f.status == FieldStatus.MISSING and f.label.strip()]
        stats.attempted = len(targets)
        if not targets:
            stats.note = "没有需要语义对齐的缺失字段"
            return stats
        if not chunks:
            stats.note = "没有可用的参考资料"
            return stats

        corpus_fp = self._corpus_fp(chunks)
        chunk_map = {c.chunk_id: c for c in chunks}

        pending: list[TemplateField] = []
        for f in targets:
            result = self._cache_get(f, corpus_fp)
            if result is None:
                pending.append(f)
                continue
            stats.cached += 1
            self._apply(f, result, chunk_map, stats)

        if pending:
            try:
                truncated_note = self._run_batches(
                    pending, chunks, question, corpus_fp, chunk_map, stats
                )
            except Exception as exc:  # 模型侧任何异常都不该让任务失败
                stats.note = f"模型调用失败，已降级为规则模式：{exc}"
                return stats
        else:
            truncated_note = ""

        summary = f"语义对齐 {stats.accepted} 项，拒绝 {stats.rejected} 项无出处取值"
        if stats.rejected == 0:
            summary = f"语义对齐 {stats.accepted} 项"
        stats.note = "；".join(n for n in (truncated_note, summary) if n)
        if getattr(self.client, "tokens", 0):
            stats.tokens = int(self.client.tokens)
        return stats

    def align_columns(
        self,
        fields: list[TemplateField],
        tables: list[DataTable],
    ) -> tuple[dict[str, str], str]:
        """把模板里的属性名映射到数据表的实际列名。

        `{活动1交付物}` 对 excel 里的 `Work Product`，字面完全不同但语义一致，
        这一步能把它救回来——而且比改词表更稳，因为列名是随表变的。
        """
        if not self.enabled or not tables:
            return {}, ""

        resolver = SequenceResolver(tables)
        placeholders = [f.placeholder for f in fields if f.placeholder]
        unresolved = resolver.unresolved(placeholders)
        if not unresolved:
            return {}, ""

        pairs: list[tuple[str, DataTable]] = []
        seen: set[str] = set()
        for _group, attr, table in unresolved:
            if attr in seen:
                continue
            seen.add(attr)
            pairs.append((attr, table))
        if not pairs:
            return {}, ""

        stats_key = ("attr", "|".join(a for a, _ in pairs), "|".join(t.header_blob for _, t in pairs))
        cached = self._cache_get_raw(stats_key)
        if cached is not None:
            return {k: v for k, v in cached.items() if v}, ""

        lines = []
        for attr, table in pairs:
            lines.append(
                f"属性：{attr}\n可选列名：{'、'.join(h for h in table.headers if h.strip())}"
            )
        user = "请为下列属性选择对应的列名（没有对应列就填 null）：\n\n" + "\n\n".join(lines)

        try:
            data = self.client.chat_json(ATTR_SYSTEM, user, max_tokens=1024)
        except Exception as exc:
            return {}, f"列名映射失败：{exc}"

        mapping: dict[str, str] = {}
        for item in data.get("mapping") or []:
            if not isinstance(item, dict):
                continue
            attr = str(item.get("attr") or "").strip()
            column = str(item.get("column") or "").strip()
            if not attr or not column:
                continue
            table = next((t for a, t in pairs if a == attr), None)
            if table is None:
                continue
            # 列名必须真实存在，模型编一个列名就等于没对上
            canonical = next((h for h in table.headers if h.strip().lower() == column.lower()), None)
            if canonical:
                mapping[attr] = canonical

        if mapping:
            self._cache_set_raw(stats_key, mapping)
        return mapping, ""

    # ---------- 批处理 ----------

    def _run_batches(
        self,
        targets: list[TemplateField],
        chunks: list[Chunk],
        question: str,
        corpus_fp: str,
        chunk_map: dict[str, Chunk],
        stats: AlignStats,
    ) -> str:
        all_batches = self._split_batches(chunks)
        batches = all_batches[: self.max_batches]
        note = ""
        if len(all_batches) > len(batches):
            note = f"资料过多，仅送前 {len(batches)} 批（共 {len(all_batches)} 批），其余请补充关键文件"
        remaining = list(targets)

        for batch in batches:
            if not remaining:
                break
            user = self._build_user(remaining, batch, question)
            data = self.client.chat_json(SYSTEM_PROMPT, user)
            by_id = {f.field_id: f for f in remaining}
            batch_map = {cid: chunk for cid, chunk, _ in batch}

            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                fid = str(item.get("field_id") or "").strip()
                field_obj = by_id.get(fid) or by_id.get(fid.upper())
                if field_obj is None:
                    continue

                result = self._verify(item, batch_map, field_obj)
                self._cache_set(field_obj, corpus_fp, result)
                self._apply(field_obj, result, chunk_map, stats)
                remaining = [f for f in remaining if f.field_id != field_obj.field_id]

        return note

    def _split_batches(self, chunks: list[Chunk]) -> list[list[tuple[str, Chunk, str]]]:
        batches: list[list[tuple[str, Chunk, str]]] = []
        current: list[tuple[str, Chunk, str]] = []
        size = 0
        for idx, chunk in enumerate(chunks, start=1):
            text = chunk.text.strip()[: self.chunk_char_limit]
            if not text:
                continue
            if current and size + len(text) > self.max_chars_per_call:
                batches.append(current)
                current, size = [], 0
            current.append((f"c{idx}", chunk, text))
            size += len(text)
        if current:
            batches.append(current)
        return batches

    def _build_user(
        self,
        fields: list[TemplateField],
        batch: list[tuple[str, Chunk, str]],
        question: str,
    ) -> str:
        parts: list[str] = []
        if question.strip():
            parts.append(f"【任务说明】\n{question.strip()[:200]}")
        parts.append(f"【待填字段】（共 {len(fields)} 个）")
        for f in fields:
            ctx = (f.hint or f.label).replace("\n", " ")[:140]
            where = f.caption or f.section or "-"
            parts.append(
                f"{f.field_id} | 字段名：{f.label} | 类型：{TYPE_CN.get(f.value_type, '短文本')}"
                f" | 位置：{where} | 上下文：{ctx}"
            )

        parts.append("\n【参考资料】")
        for cid, chunk, text in batch:
            parts.append(f"[{cid}] 《{chunk.doc_name}》· {chunk.locator}\n{text}")

        parts.append(
            "\n请逐个字段判断，输出 JSON。找不到依据的字段 value 必须为 null 并说明缺什么。"
        )
        return "\n".join(parts)

    # ---------- 校验 ----------

    def _verify(
        self,
        item: dict[str, Any],
        batch_map: dict[str, Chunk],
        field_obj: TemplateField,
    ) -> FieldResult:
        value = str(item.get("value") or "").strip()
        quote = str(item.get("quote") or "").strip()
        chunk_id = str(item.get("evidence_chunk") or "").strip()
        reason = str(item.get("reason") or "").strip()

        if value.lower() in ("null", "none", "无", ""):
            return FieldResult(accepted=False, reason=reason or "资料中未找到对应内容")

        chunk = batch_map.get(chunk_id)
        if chunk is None:
            return FieldResult(
                accepted=False,
                reason=f"模型引用了不存在的资料块「{chunk_id}」，取值已拒绝",
            )
        if len(quote) < 6:
            return FieldResult(
                accepted=False,
                reason="模型未给出可核验的原文片段，取值已拒绝",
            )
        if _norm(quote) not in _norm(chunk.text):
            return FieldResult(
                accepted=False,
                reason=f"模型引用的原文在《{chunk.doc_name}》{chunk.locator}中找不到（疑似编造），已拒绝",
            )
        if not is_traceable(value, chunk.text, field_obj.value_type):
            return FieldResult(
                accepted=False,
                reason=f"模型给出的取值在原文中找不到出处，已拒绝（{chunk.locator}）",
            )

        try:
            confidence = float(item.get("confidence") or 0.7)
        except (TypeError, ValueError):
            confidence = 0.7
        return FieldResult(
            accepted=True,
            value=value,
            chunk_id=chunk.chunk_id,
            quote=quote,
            confidence=max(0.5, min(0.9, confidence)),
            reason=reason,
        )

    # ---------- 结果落地 ----------

    def _apply(
        self,
        field_obj: TemplateField,
        result: FieldResult,
        chunk_map: dict[str, Chunk],
        stats: AlignStats,
    ) -> None:
        if result.accepted:
            chunk = chunk_map.get(result.chunk_id)
            if chunk is None:
                return
            field_obj.status = FieldStatus.PENDING
            field_obj.value = result.value
            field_obj.evidence = [result.chunk_id]
            field_obj.confidence = result.confidence
            note = "（模型归纳，非逐字）" if field_obj.value_type == ValueType.LONG_TEXT else ""
            field_obj.basis = (
                f"语义对齐：《{chunk.doc_name}》{chunk.locator} 原文「{result.quote[:70]}」"
                f"{note}——模型给出候选值，请核对"
            )
            field_obj.question = ""
            stats.accepted += 1
            return

        if "已拒绝" in result.reason or "不存在" in result.reason:
            stats.rejected += 1
        else:
            stats.empty += 1
        field_obj.status = FieldStatus.MISSING
        field_obj.value = ""
        field_obj.evidence = []
        field_obj.confidence = 0.0
        detail = (
            f"模型复核后仍未找到：{result.reason}。"
            if result.reason
            else "模型复核后仍未找到可引用的原文。"
        )
        field_obj.basis = result.reason or "模型复核后未找到依据"
        field_obj.question = build_question(field_obj, detail)

    # ---------- 缓存 ----------

    @staticmethod
    def _corpus_fp(chunks: Iterable[Chunk]) -> str:
        h = hashlib.sha256()
        for c in chunks:
            h.update(c.chunk_id.encode("utf-8"))
            h.update(c.text.encode("utf-8"))
        return h.hexdigest()[:16]

    def _field_key(self, field_obj: TemplateField, corpus_fp: str) -> str:
        payload = "|".join(
            [
                corpus_fp,
                field_obj.label,
                field_obj.hint,
                field_obj.value_type.value,
                field_obj.placeholder,
            ]
        )
        return ResultCache.make_key("field", payload)

    def _cache_get(self, field_obj: TemplateField, corpus_fp: str) -> FieldResult | None:
        if not self.cache:
            return None
        raw = self.cache.get(self._field_key(field_obj, corpus_fp))
        if not isinstance(raw, dict):
            return None
        try:
            return FieldResult(
                accepted=bool(raw["accepted"]),
                value=str(raw.get("value") or ""),
                chunk_id=str(raw.get("chunk_id") or ""),
                quote=str(raw.get("quote") or ""),
                confidence=float(raw.get("confidence") or 0.0),
                reason=str(raw.get("reason") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _cache_set(self, field_obj: TemplateField, corpus_fp: str, result: FieldResult) -> None:
        if not self.cache:
            return
        self.cache.set(
            self._field_key(field_obj, corpus_fp),
            {
                "accepted": result.accepted,
                "value": result.value,
                "chunk_id": result.chunk_id,
                "quote": result.quote,
                "confidence": result.confidence,
                "reason": result.reason,
            },
        )

    def _cache_get_raw(self, parts: tuple[str, ...]) -> dict[str, str] | None:
        if not self.cache:
            return None
        raw = self.cache.get(ResultCache.make_key(*parts))
        return raw if isinstance(raw, dict) else None

    def _cache_set_raw(self, parts: tuple[str, ...], value: dict[str, str]) -> None:
        if not self.cache:
            return
        self.cache.set(ResultCache.make_key(*parts), value)
