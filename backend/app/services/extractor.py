"""从证据片段中抽取候选值。

这一层是「规则下限」：在接大模型之前，先把「核心词 + 冒号 + 值」这种
最常见的写法抽出来，让候选值至少有个初值，用户只需要核对而不是从零填写。

M2 接模型后，这里退化为兜底——模型抽不出值时仍会用规则结果，
两边都抽不出，才落回「需补充」。
"""
from __future__ import annotations

import re

from ..core.models import ValueType

VALUE_STOP = "。；;\n"
DATE_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]?\s*\d{0,2}\s*日?")
TAIL_RE = re.compile(r"[^。；;\n]{1,120}")


def extract_value(core: str, text: str, value_type: ValueType = ValueType.TEXT) -> str:
    if not core:
        return ""

    raw = ""
    # 首选：核心词紧跟冒号
    match = re.search(re.escape(core) + r"\s*[：:]\s*(" + TAIL_RE.pattern + ")", text)
    if match:
        raw = match.group(1).strip()
    else:
        # 退一步：核心词后面跟空格分隔的内容（「项目名称 电池控制器」）
        match = re.search(re.escape(core) + r"\s+(" + TAIL_RE.pattern + ")", text)
        if match:
            raw = match.group(1).strip()

    if not raw:
        return ""

    # 同句里往往串联了多个字段，取到第一个分隔符为止
    for sep in ("，", ",", "（", "("):
        if sep in raw:
            head = raw.split(sep)[0].strip()
            # 括号紧跟在值后面时保留主体即可
            if len(head) >= 1:
                raw = head

    if value_type == ValueType.DATE:
        dm = DATE_RE.search(raw)
        if dm:
            return dm.group(0).strip()

    raw = raw.strip(" \t：:")
    return raw[:120]
