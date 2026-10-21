"""导出器：把判定结果写回模板，并附上依据索引与待补充清单。

两条硬规矩：
1. 模板原有格式不动——只往空格里塞 run，不重建单元格，否则用户的公文样式就废了。
2. 没依据的字段一律写入醒目的占位提示，绝不留白，也绝不编造。
   留白最危险：看起来像"已经写完了"，实际上漏了东西没人发现。
"""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.text.paragraph import Paragraph

from ..parsers.placeholder import PH_RE, VALID_NAME
from .dataset import SEQ_RE

PLACEHOLDER = "【待补充：{reason}】"
STATUS_LABEL = {
    "filled": "已填",
    "pending_confirm": "待确认",
    "missing": "需补充",
}
# 合并单元格的行不能安全复制/删除：跨行合并一拆，整张表就错位
MERGE_TAGS = (qn("w:vMerge"), qn("w:gridSpan"))


class DocxExporter:
    def render(
        self,
        template_path: Path,
        fields: list[dict],
        output_path: Path,
        task_id: str = "",
    ) -> Path:
        doc = Document(str(template_path))
        written = 0

        placeholder_fields = [f for f in fields if f.get("kind") == "placeholder"]
        if placeholder_fields:
            # 顺序不能反：先把表格行数调成数据条数，再填值。
            # 反过来新行里就没有值可填，会留一排占位符。
            self._sync_table_rows(doc, placeholder_fields)
            written = self._render_placeholders(doc, placeholder_fields)
        else:
            for f in fields:
                value = self._resolve_value(f)
                if value is None:
                    continue
                if f.get("kind") == "table_cell":
                    written += self._write_cell(doc, f, value)
                else:
                    written += self._write_paragraph(doc, f, value)

        self._append_appendix(doc, fields, task_id, written)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(output_path))
        return output_path

    def _resolve_value(self, field: dict) -> str | None:
        """返回要写入的文本；None 表示保持模板原样（占位符不被替换）。"""
        value = (field.get("value") or "").strip()
        if value:
            return value
        # 留白最危险：看起来像写完了，实际漏了东西没人发现。
        # 缺依据和待确认都要留下醒目的痕迹。
        status = field.get("status")
        if status == "missing":
            return PLACEHOLDER.format(reason=self._short_label(field))
        if status == "pending_confirm":
            return f"【待确认：{self._short_label(field)}】"
        return None

    # ---------- 按数据条数调整表格行 ----------

    def _sync_table_rows(self, doc, fields: list[dict]) -> int:
        """把集合表格的行数调成字段清单要求的行数。

        字段清单是唯一事实来源：expander 已经按数据条数把 `{活动5名称}` 这类
        字段补齐或裁掉，这里只要让文档跟上字段清单即可。
        这样「文档行数」和「字段清单」不会各说各话。
        """
        targets: dict[tuple[int, str], int] = {}
        for f in fields:
            table_index = f.get("table_index")
            placeholder = (f.get("placeholder") or "").strip()
            if table_index is None or not placeholder:
                continue
            match = SEQ_RE.match(placeholder)
            if not match:
                continue
            key = (table_index, match.group(1))
            targets[key] = max(targets.get(key, 0), int(match.group(2)))
        if not targets:
            return 0

        changed = 0
        for (table_index, group), target in targets.items():
            if table_index >= len(doc.tables):
                continue
            table = doc.tables[table_index]
            # 每轮按最新结构重扫：增删行会改行号，用旧行号会错位
            for _ in range(3):
                seq_rows, ph_count = self._scan_group(table, group)
                if not seq_rows:
                    break
                current = max(seq_rows)
                if current == target:
                    break
                if current < target:
                    changed += self._grow(table, seq_rows, ph_count, group, target)
                else:
                    changed += self._shrink(table, seq_rows, group, target)
        return changed

    def _scan_group(self, table, group: str) -> tuple[dict[int, int], dict[int, int]]:
        """扫出某个集合在表格里的位置：序号→行号，行号→该行占位符个数。"""
        seq_rows: dict[int, int] = {}
        ph_count: dict[int, int] = {}
        for row_index, row in enumerate(table.rows):
            names = self._row_placeholders(row)
            seqs = set()
            for name in names:
                match = SEQ_RE.match(name)
                if match and match.group(1) == group:
                    seqs.add(int(match.group(2)))
            if not seqs:
                continue
            ph_count[row_index] = len(names)
            for seq in seqs:
                seq_rows.setdefault(seq, row_index)
        return seq_rows, ph_count

    @classmethod
    def _row_placeholders(cls, row) -> list[str]:
        names: list[str] = []
        seen: set = set()
        for cell in row.cells:
            if cell._tc in seen:        # 合并单元格会重复返回同一个 tc
                continue
            seen.add(cell._tc)
            for para in cell.paragraphs:
                for match in PH_RE.finditer(para.text or ""):
                    name = (match.group(1) or match.group(2) or "").strip()
                    if name:
                        names.append(name)
        return names

    def _grow(self, table, seq_rows: dict[int, int], ph_count: dict[int, int],
              group: str, target: int) -> int:
        """复制占位符最全的那一行，逐行接在它后面并改序号。

        为什么挑「占位符最多」而不是「最后一行」：模板里偶尔有某行漏写占位符，
        复制漏写的那行，扩出来的行就少一列值，而且不会有任何报错。
        """
        if not ph_count:
            return 0
        src_row = max(ph_count, key=lambda r: (ph_count[r], r))
        src_seq = next((s for s, r in seq_rows.items() if r == src_row), None)
        if src_seq is None:
            return 0

        tr = table.rows[src_row]._tr
        if self._has_merge(tr):
            return 0                    # 带合并的行复制必错位，宁可少填不搞乱版式

        anchor = tr
        added = 0
        for seq in range(max(seq_rows) + 1, target + 1):
            new_tr = deepcopy(tr)
            anchor.addnext(new_tr)
            anchor = new_tr
            self._renumber(new_tr, group, src_seq, seq)
            self._bump_row_no(new_tr, src_seq, seq)
            added += 1
        return added

    @staticmethod
    def _bump_row_no(tr_element, src_seq: int, dst_seq: int) -> None:
        """模板里的「序号」列常常是写死的 1、2、3，不是占位符。

        复制第 4 行放大成第 5 行后，序号列还是「4」——一行里两个 4 很难看，
        也很难解释。所以首列若是纯数字且正好等于源行序号，就跟着递增。
        只认首列 + 必须等于源序号，两个条件卡住，避免误改「人数=4」这类列。
        """
        cells = tr_element.findall(qn("w:tc"))
        if not cells:
            return
        for p_element in cells[0].iter(qn("w:p")):
            para = Paragraph(p_element, None)
            text = (para.text or "").strip()
            if text != str(src_seq):
                continue
            runs = para.runs
            if not runs:
                continue
            runs[0].text = str(dst_seq)
            for extra in runs[1:]:
                extra.text = ""
            return

    def _shrink(self, table, seq_rows: dict[int, int], group: str, target: int) -> int:
        """数据比模板行数少时删掉多余行，避免导出后拖一串「待补充」。"""
        drop = [r for s, r in seq_rows.items() if s > target]
        removed = 0
        for row_index in sorted(set(drop), reverse=True):
            tr = table.rows[row_index]._tr
            if self._has_merge(tr):
                continue
            tr.getparent().remove(tr)
            removed += 1
        return removed

    @staticmethod
    def _has_merge(tr) -> bool:
        return any(tr.findall(".//" + tag) for tag in MERGE_TAGS)

    @classmethod
    def _renumber(cls, tr_element, group: str, src_seq: int, dst_seq: int) -> None:
        """把复制行里的 `{活动4名称}` 改成 `{活动5名称}`。

        Word 同样会把占位符拆进多个 run，所以还是先拼合再写回。
        """
        for p_element in tr_element.iter(qn("w:p")):
            para = Paragraph(p_element, None)
            runs = para.runs
            if not runs:
                continue
            full = "".join(r.text for r in runs)
            if "{" not in full:
                continue

            def _sub(match: re.Match) -> str:
                name = (match.group(1) or match.group(2) or "").strip()
                hit = SEQ_RE.match(name)
                if hit and hit.group(1) == group and int(hit.group(2)) == src_seq:
                    return "{" + f"{group}{dst_seq}{hit.group(3)}" + "}"
                return match.group(0)

            new_text = PH_RE.sub(_sub, full)
            if new_text == full:
                continue
            runs[0].text = new_text
            for extra in runs[1:]:
                extra.text = ""

    # ---------- 占位符替换 ----------

    def _render_placeholders(self, doc, fields: list[dict]) -> int:
        mapping: dict[str, str] = {}
        for f in fields:
            name = (f.get("placeholder") or "").strip()
            if not name:
                continue
            value = self._resolve_value(f)
            if value is not None:
                mapping[name] = value
        if not mapping:
            return 0
        return self._replace_all(doc, mapping)

    def _replace_all(self, doc, mapping: dict[str, str]) -> int:
        count = 0
        for para in self._iter_paragraphs(doc):
            count += self._replace_paragraph(para, mapping)
        return count

    @staticmethod
    def _iter_paragraphs(doc):
        for p in doc.paragraphs:
            yield p
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        yield p
        for sec in doc.sections:
            for hf in (sec.header, sec.footer):
                for p in hf.paragraphs:
                    yield p

    @classmethod
    def _replace_paragraph(cls, para, mapping: dict[str, str]) -> int:
        """Word 常把 {项目名称} 拆到多个 run 里，所以要先拼合再替换。

        替换后统一写入第一个 run：保留该段落的字体字号，代价是段内混排格式
        会被抹平。模板里的占位符基本都是同格式，这个代价可以接受。
        """
        runs = para.runs
        if not runs:
            return 0
        full = "".join(r.text for r in runs)
        if "{" not in full:
            return 0

        def _sub(match: re.Match) -> str:
            name = (match.group(1) or match.group(2) or "").strip()
            if name in mapping:
                return mapping[name]
            # 兜底：字段清单里没有这个名字（扩行失败、合并单元格跳过等），
            # 也绝不能留一个裸 {占位符} 混进正文冒充已填。
            if VALID_NAME.match(name):
                return PLACEHOLDER.format(reason=name)
            return match.group(0)

        new_text = PH_RE.sub(_sub, full)
        if new_text == full:
            return 0
        runs[0].text = new_text
        for extra in runs[1:]:
            extra.text = ""
        return 1

    # ---------- 写回 ----------

    @staticmethod
    def _short_label(field: dict) -> str:
        label = field.get("label") or "该项"
        return label.split("：")[-1].split(" / ")[-1]

    @staticmethod
    def _write_cell(doc, field: dict, value: str) -> int:
        t_i, r_i, c_i = field.get("table_index"), field.get("row_index"), field.get("col_index")
        if None in (t_i, r_i, c_i):
            return 0
        try:
            cell = doc.tables[t_i].rows[r_i].cells[c_i]
        except (IndexError, KeyError):
            return 0
        para = cell.paragraphs[0]
        if para.runs:
            para.runs[0].text = value
            for extra in para.runs[1:]:
                extra.text = ""
        else:
            para.add_run(value)
        return 1

    @staticmethod
    def _write_paragraph(doc, field: dict, value: str) -> int:
        p_i = field.get("paragraph_index")
        if p_i is None or p_i >= len(doc.paragraphs):
            return 0
        para = doc.paragraphs[p_i]
        para.add_run(value)
        return 1

    # ---------- 附录 ----------

    def _append_appendix(self, doc, fields: list[dict], task_id: str, written: int) -> None:
        doc.add_page_break()

        title = doc.add_heading("附录 A　填写依据索引", level=1)
        title.alignment = WD_ALIGN_PARAGRAPH.LEFT

        intro = doc.add_paragraph()
        run = intro.add_run(
            f"本附录由文件处理 Agent 自动生成（任务号 {task_id or '-'}）。"
            f"正文共 {written} 项由参考资料自动填充，每项均可回溯至原文位置；"
            "标注「需补充」的项未在资料中找到依据，未作推测。"
        )
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x5F, 0x5E, 0x5A)

        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        for i, name in enumerate(["字段", "状态", "依据位置", "判定说明"]):
            cell = table.rows[0].cells[i]
            cell.text = name
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True

        for f in fields:
            row = table.add_row().cells
            row[0].text = f.get("label", "")
            row[1].text = STATUS_LABEL.get(f.get("status", ""), f.get("status", ""))
            row[2].text = "、".join(f.get("evidence") or []) or "—"
            row[3].text = f.get("basis", "")

        missing = [f for f in fields if f.get("status") == "missing"]
        if missing:
            doc.add_heading("附录 B　待补充事项", level=1)
            doc.add_paragraph(
                "下列内容在已提供的资料中无依据支撑，需由编制人补充。"
                "补充后可重新生成，相关字段将自动更新。"
            )
            for idx, f in enumerate(missing, start=1):
                para = doc.add_paragraph(style="List Number")
                label_run = para.add_run(f"{f.get('label', '')}：")
                label_run.bold = True
                para.add_run(f.get("question", "缺少填写依据"))
