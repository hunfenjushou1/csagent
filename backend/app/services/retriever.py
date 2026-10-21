"""字段级证据判定（不含检索增强）。

**这里刻意不做 RAG**：不建向量库、不做 BM25 召回、不把片段喂给模型生成。
原因不是省事，而是 RAG 的召回环节本身会制造假阳性——「网络安全接口人」和
「网络安全接口协议」共享大半词汇，BM25 会把后者排到前面，看起来"检索到了"，
实际是两个完全不同的东西。本项目的要求是「找不到依据就直接问用户」，
那么任何模糊匹配都是负债。

改为两条确定性路径，命中与否都可解释：
1. **结构化对齐**：`{活动1名称}` → excel 某行某列，直接读单元格（见 dataset.py）
2. **精确抽取**：全量扫描参考文件，只认「字段名完整出现且后面真的带出了值」
两条都没有 → 判缺失，生成具体追问。

没有召回环节，就不存在"排在后面的正确片段被挤掉"的问题，召回率是 100%；
也不需要中文分词和语料索引，少两个依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.models import Chunk, FieldStatus, TemplateField, ValueType
from .dataset import DataTable, SequenceResolver
from .extractor import extract_value
from .questions import build_question

# label 里的结构噪音：这些词在模板里出现，但不携带业务语义
LABEL_NOISE = re.compile(r"(序号|备注|填写|请填|待填|（.{0,10}）|\(.{0,10}\))")
SPLIT_RE = re.compile(r"[／/、，,；;（）()\[\]【】：:]")

STRONG = 0.75   # 字段名完整出现且带出取值，可直接给用户看候选
WEAK = 0.5      # 部分字面重合，仅作解释，仍判缺失
NOTE = 0.15     # 基本无关，不作为解释


@dataclass
class Hit:
    chunk: Chunk
    confidence: float
    reason: str
    value: str = ""      # 抽出的候选值，判定和取值共用同一次抽取结果


class FieldRetriever:
    def __init__(
        self,
        chunks: list[Chunk],
        top_k: int = 5,
        tables: list[DataTable] | None = None,
        attr_map: dict[str, str] | None = None,
    ) -> None:
        self.chunks = [c for c in chunks if c.text.strip()]
        self.top_k = top_k
        self.sequence = SequenceResolver(tables or [], attr_map=attr_map)

    # ---------- 文本处理 ----------

    @staticmethod
    def label_core(field: TemplateField) -> str:
        """表格字段形如「行标签 / 列名」，取列名；段落字段直接取 label。"""
        core = field.label.split(" / ")[-1]
        return LABEL_NOISE.sub("", core).strip() or core.strip()

    @staticmethod
    def field_terms(field: TemplateField) -> list[str]:
        """按分隔符切出 label 里的业务词，**不做中文分词**。

        分词会把「网络安全负责人」切成「网络安全」+「负责人」，于是「网络安全接口协议」
        能命中一半、看着像有依据——正是要避免的假阳性。所以宁可认整词，
        整词不出现就直接判缺失，由用户来补。
        """
        raw = LABEL_NOISE.sub(" ", field.label)
        terms = [t.strip() for t in SPLIT_RE.split(raw) if len(t.strip()) >= 2]
        seen: set[str] = set()
        return [t for t in terms if not (t in seen or seen.add(t))]

    @staticmethod
    def _overlap(core: str, text: str, limit: int = 3) -> list[str]:
        """core 与原文的字面重合片段，只用来解释「为什么这不算依据」。

        用 2-gram 而不是分词：不需要词典，且能明确指出重合的是哪两个字，
        用户一眼就知道是「网络安全接口协议」蹭到了「网络安全接口人」。
        """
        out: list[str] = []
        for i in range(len(core) - 1):
            gram = core[i:i + 2]
            if gram in text and gram not in out:
                out.append(gram)
                if len(out) >= limit:
                    break
        return out

    # ---------- 判定 ----------

    def _confidence(self, field: TemplateField, chunk: Chunk) -> tuple[float, str, str]:
        """只有字段名完整出现、且后面确实带出了内容，才算强证据。"""
        core = self.label_core(field)
        text = chunk.text
        if not core:
            return NOTE, f"{chunk.locator}无有效字段名", ""

        if core in text:
            # 先做一次廉价的子串判断，避免对每个 chunk 都跑正则
            value = extract_value(core, text, field.value_type)
            if value:
                return 1.0, f"「{core}」在{chunk.locator}处有对应取值「{value}」", value
            return 0.7, f"「{core}」出现在{chunk.locator}，但该处未带出取值", ""

        overlap = self._overlap(core, text)
        if overlap:
            return 0.45, (
                f"{chunk.locator}出现「{'、'.join(overlap)}」，"
                f"但未出现完整的「{core}」，不能作为依据"
            ), ""
        return NOTE, f"与{chunk.locator}无关", ""

    def search(self, field: TemplateField, k: int | None = None) -> list[Hit]:
        """全量扫描所有语料块，返回按证据强度排序的命中。

        没有召回、没有阈值过滤：几百块的规模下全量扫描是毫秒级的，
        而任何"先粗排再精判"都会在粗排阶段丢掉正确片段。
        """
        hits = [
            Hit(chunk=chunk, confidence=conf, reason=reason, value=value)
            for chunk in self.chunks
            for conf, reason, value in [self._confidence(field, chunk)]
            if conf > NOTE
        ]
        hits.sort(key=lambda h: h.confidence, reverse=True)
        return hits[: (k or self.top_k)]

    # ---------- 填充判定 ----------

    def fill_fields(
        self,
        fields: list[TemplateField],
        extra_answers: dict[str, str] | None = None,
    ) -> list[TemplateField]:
        """逐字段判定三态。

        extra_answers 是用户在审阅页补充的回答。用户回答本身就是证据来源
        （locator 记为用户补充），否则会出现「用户明明答了还提示缺失」的荒唐情况。
        """
        extra_answers = extra_answers or {}
        for f in fields:
            answer = (extra_answers.get(f.field_id) or "").strip()
            if answer:
                f.status = FieldStatus.PENDING
                f.value = answer
                f.evidence = [f"user-answer:{f.field_id}"]
                f.confidence = 1.0
                f.basis = "依据来自用户本人补充"
                f.question = ""
                continue

            # 结构化表格优先：{活动1名称} 能直接落到 excel 第 1 行，比任何文本匹配可靠
            seq_hit = self.sequence.resolve(f.placeholder) if f.placeholder else None
            if seq_hit:
                f.status = FieldStatus.FILLED
                f.value = seq_hit.value
                f.confidence = 1.0
                f.basis = seq_hit.basis
                f.question = ""
                continue

            terms = self.field_terms(f)
            if not terms:
                self._auto_or_missing(f, "字段名不含可检索的业务词")
                continue

            hits = self.search(f)
            best = hits[0] if hits else None

            if best and best.confidence >= STRONG:
                f.status = FieldStatus.PENDING
                f.value = best.value
                f.evidence = [h.chunk.chunk_id for h in hits if h.confidence >= STRONG][:3]
                f.confidence = best.confidence
                f.basis = best.reason + (
                    "；已抽取候选值，请核对" if best.value else "；未抽取出具体取值，需人工填写"
                )
                f.question = ""
            else:
                f.status = FieldStatus.MISSING
                f.evidence = []
                f.confidence = best.confidence if best else 0.0
                f.basis = best.reason if best else "全部参考文件中未检索到相关内容"
                f.value = ""
                f.question = self._build_question(f, best, terms)
        return fields

    def _auto_or_missing(self, field: TemplateField, reason: str) -> None:
        """纯「序号」这类字段无需检索，按行序自动生成。"""
        if "序号" in field.label and field.row_index is not None:
            field.status = FieldStatus.FILLED
            field.value = str(field.row_index)
            field.confidence = 1.0
            field.basis = "序号按表格行序自动生成，无需检索"
            field.question = ""
            return
        field.status = FieldStatus.MISSING
        field.confidence = 0.0
        field.basis = reason
        field.question = self._build_question(field, None, [])

    def _build_question(self, field: TemplateField, best: Hit | None, terms: list[str]) -> str:
        if best is not None and best.confidence > 0:
            detail = f"现有资料中仅有弱相关片段（{best.reason}），不足以支撑填写。"
        else:
            detail = "已上传的文件中未找到可引用的原文。"
        return build_question(field, detail, terms)


def evidence_detail(retriever: FieldRetriever, field: TemplateField) -> list[dict]:
    """给前端核验用：展示字段命中的片段及原文定位。"""
    return [
        {
            "chunk_id": h.chunk.chunk_id,
            "doc_name": h.chunk.doc_name,
            "locator": h.chunk.locator,
            "confidence": round(h.confidence, 2),
            "reason": h.reason,
            "text": h.chunk.text[:220],
        }
        for h in retriever.search(field)
    ]
