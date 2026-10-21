"""缺口追问文案。

规则层判定缺失、模型复核也找不到，两种情况下给用户的话术必须一致——
否则同一个字段会因为触发路径不同，时而说「请填写日期」，时而说「请上传清单」。
"""
from __future__ import annotations

from ..core.models import TemplateField, ValueType

TIPS: dict[ValueType, str] = {
    ValueType.DATE: "请给出具体日期（如 2026-03-01）",
    ValueType.LIST: "请列出具体条目，或上传包含该清单的文件（如成员名单、活动清单）",
    ValueType.LONG_TEXT: "请描述具体内容，或上传包含该章节的源文件",
    ValueType.NUMBER: "请给出具体数值",
    ValueType.TEXT: "请直接填写，或上传包含该项信息的文件",
}


def tip_for(field: TemplateField) -> str:
    if field.label.startswith("勾选项"):
        return "请在模板给出的选项中勾选适用项（可多选）"
    return TIPS.get(field.value_type, "请直接填写")


def build_question(field: TemplateField, detail: str, terms: list[str] | None = None) -> str:
    """detail 说明「为什么没找到」，terms 说明「到底在找什么」。

    跳过 detail 直接说"信息不足"是没用的——用户不知道该补哪份材料。
    """
    where = field.caption or field.section or "模板"
    text = f"「{field.label}」缺少填写依据。该字段位于「{where}」。{detail}"
    related = "、".join((terms or [])[:4])
    if related:
        text += f"需要查找的内容：{related}。"
    return text + tip_for(field)
