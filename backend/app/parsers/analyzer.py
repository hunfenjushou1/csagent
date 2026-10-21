"""模板解析统一入口。

两份模板可能长得完全不一样，但对上层都是 TemplateSpec：
- 占位符模板（{项目名称}）：直接读名字，精确定位
- 结构模板（空白表格/空白段落）：靠结构推断，有误差
自动探测走哪条路，也允许调用方显式指定。
"""
from __future__ import annotations

from pathlib import Path

from ..core.models import TemplateSpec
from .docx_template import DocxTemplateAnalyzer
from .placeholder import PlaceholderAnalyzer, count_placeholders

# 低于这个数量说明文档里只是零星花括号（公式、代码片段），不足以判定为占位符模板
PLACEHOLDER_THRESHOLD = 3

MODE_LABEL = {
    "placeholder": "占位符",
    "structure": "结构推断",
}


def detect_mode(path: str | Path) -> str:
    return "placeholder" if count_placeholders(path) >= PLACEHOLDER_THRESHOLD else "structure"


def analyze_template(path: str | Path, name: str = "", mode: str = "auto") -> TemplateSpec:
    path = Path(path)
    resolved = detect_mode(path) if mode == "auto" else mode

    if resolved == "placeholder":
        spec = PlaceholderAnalyzer().analyze(path, name)
    else:
        spec = DocxTemplateAnalyzer().analyze(path, name)

    spec.source = f"local:{resolved}"
    return spec
