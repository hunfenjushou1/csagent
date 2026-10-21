"""文件处理 Agent 后端服务。

处理链路：上传 → 模板拆字段 → 结构化对齐 → 精确抽取 → 语义对齐（模型）
→ 仍无依据则向用户追问 → 导出。

模型只负责"找依据"，能不能用由代码校验（见 aligner._verify）。
未配置 Key 时整条链路自动降级为纯规则模式，不报错、不阻塞。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.models import FieldStatus, ProcessingReport
from app.parsers.doc_parser import DocumentParser
from app.parsers.analyzer import analyze_template
from app.services.aligner import SemanticAligner
from app.services.dataset import DatasetBuilder
from app.services.expander import expand_fields
from app.services.exporter import DocxExporter
from app.services.llm import DeepSeekClient, LLMSettings, ResultCache
from app.services.retriever import FieldRetriever, evidence_detail

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "storage"
TASKS = STORAGE / "tasks"
TASKS.mkdir(parents=True, exist_ok=True)

LLM_SETTINGS = LLMSettings.from_env(ROOT)
LLM_CACHE_DIR = LLM_SETTINGS.cache_dir or (STORAGE / ".llm_cache")
RESULT_CACHE = ResultCache(LLM_CACHE_DIR / "results.json")

app = FastAPI(title="文件处理 Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = ROOT / "web"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_index: dict[str, dict] = {}


def _make_aligner() -> SemanticAligner:
    client = DeepSeekClient(LLM_SETTINGS) if LLM_SETTINGS.enabled else None
    return SemanticAligner(client, cache=RESULT_CACHE)


def _task_dir(task_id: str) -> Path:
    d = TASKS / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_upload(upload: UploadFile, target_dir: Path) -> Path:
    name = Path(upload.filename or "unnamed").name
    dest = target_dir / name
    with dest.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return dest


def _build_payload(report: ProcessingReport, retriever: FieldRetriever) -> dict:
    data = report.to_dict()
    chunk_map = {c.chunk_id: c for c in report.chunks}
    by_field = {}
    for f in report.fields:
        details = []
        for cid in f.evidence:
            chunk = chunk_map.get(cid)
            if chunk:
                details.append(
                    {
                        "chunk_id": cid,
                        "doc_name": chunk.doc_name,
                        "locator": chunk.locator,
                        "text": chunk.text[:400],
                    }
                )
        if not details and f.status == FieldStatus.MISSING:
            details = evidence_detail(retriever, f)[:2]
        by_field[f.field_id] = details
    data["evidence"] = by_field
    return data


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = STATIC_DIR / "index.html"
    if not page.exists():
        raise HTTPException(500, "前端未构建，请检查 web/index.html")
    return HTMLResponse(page.read_text(encoding="utf-8"))


def _run_pipeline(
    task_id: str,
    tpl_path: Path,
    ref_paths: list[Path],
    question: str,
    extra_answers: dict[str, str] | None = None,
    skipped: list[str] | None = None,
) -> dict:
    """analyze 与 answers 共用同一条链路，避免两处判定规则漂移。"""
    workdir = _task_dir(task_id)
    spec = analyze_template(tpl_path)

    parser = DocumentParser()
    chunks = []
    skip = list(skipped or [])
    for path in ref_paths:
        try:
            chunks.extend(parser.parse(path))
        except ValueError as exc:
            skip.append(f"{path.name}: {exc}")

    tables = DatasetBuilder().build(ref_paths)
    # 按数据条数调整集合字段（活动表 5 条、模板只有 4 行 → 补 {活动5…}）
    spec.fields, expand_plan = expand_fields(spec.fields, tables)

    aligner = _make_aligner()
    # 属性名 → 数据表列名的语义映射（{活动1交付物} 对 excel 的 Work Product）
    attr_map, attr_note = aligner.align_columns(spec.fields, tables)

    retriever = FieldRetriever(chunks, tables=tables, attr_map=attr_map)
    retriever.fill_fields(spec.fields, extra_answers=extra_answers)

    # 规则判不出来的字段交给模型做语义对齐；模型给的每个值都要过代码校验
    align_stats = aligner.align(spec.fields, chunks, question=question)

    report = ProcessingReport(
        task_id=task_id, template_name=spec.name, fields=spec.fields, chunks=chunks
    )
    (workdir / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    payload = _build_payload(report, retriever)
    payload["ref_files"] = [p.name for p in ref_paths]
    payload["skipped"] = skip
    payload["template_fingerprint"] = spec.source_ref
    payload["template_mode"] = spec.source.split(":")[-1]
    payload["chunk_count"] = len(chunks)
    payload["expansion"] = [c.to_dict() for c in expand_plan]
    payload["llm"] = {
        **align_stats.to_dict(),
        # 未启用的模型也要显示出来，否则前端只能看到空白，无法确认配置对不对
        "model": align_stats.model or LLM_SETTINGS.model,
        "column_map": attr_map,
        "column_note": attr_note,
    }
    return payload


@app.post("/api/analyze")
async def analyze(
    template: UploadFile = File(...),
    files: list[UploadFile] = File(default=[]),
    question: str = Form(default=""),
) -> dict:
    task_id = uuid.uuid4().hex[:12]
    workdir = _task_dir(task_id)
    tpl_path = _save_upload(template, workdir)
    if tpl_path.suffix.lower() != ".docx":
        raise HTTPException(400, "模板目前仅支持 .docx")

    ref_paths = [_save_upload(f, workdir) for f in files if f.filename]

    _index[task_id] = {
        "template_path": str(tpl_path),
        "question": question,
        "created_at": time.time(),
        "ref_files": [p.name for p in ref_paths],
    }
    return _run_pipeline(task_id, tpl_path, ref_paths, question)


@app.post("/api/answers")
async def answers(task_id: str = Form(...), answers: str = Form(...)) -> dict:
    """用户在审阅页补充的回答，作为新证据回流，重新判定三态。"""
    meta = _index.get(task_id)
    if not meta:
        raise HTTPException(404, "任务不存在或已过期")

    try:
        parsed: dict[str, str] = json.loads(answers)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "answers 必须是 JSON 对象") from exc

    workdir = _task_dir(task_id)
    tpl_path = Path(meta["template_path"])
    ref_paths = [workdir / name for name in meta["ref_files"]]
    ref_paths = [p for p in ref_paths if p.exists()]

    return _run_pipeline(
        task_id, tpl_path, ref_paths, meta.get("question", ""), extra_answers=parsed
    )


@app.post("/api/export")
def export(task_id: str = Form(...)) -> FileResponse:
    meta = _index.get(task_id)
    if not meta:
        raise HTTPException(404, "任务不存在或已过期")

    workdir = _task_dir(task_id)
    report_file = workdir / "report.json"
    if not report_file.exists():
        raise HTTPException(400, "该任务尚未生成结果")

    data = json.loads(report_file.read_text(encoding="utf-8"))
    out_path = workdir / f"{Path(meta['template_path']).stem}-已填充.docx"
    DocxExporter().render(
        template_path=Path(meta["template_path"]),
        fields=data["fields"],
        output_path=out_path,
        task_id=task_id,
    )
    return FileResponse(
        str(out_path),
        filename=out_path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "tasks": len(_index),
        "llm": {"enabled": LLM_SETTINGS.enabled, "model": LLM_SETTINGS.model},
    }


@app.get("/api/llm/status")
def llm_status() -> dict:
    """前端用来显示「当前是不是模型模式」，也方便排查为什么没走语义对齐。"""
    return {
        "enabled": LLM_SETTINGS.enabled,
        "model": LLM_SETTINGS.model,
        "base_url": LLM_SETTINGS.base_url,
        "cache_dir": str(LLM_CACHE_DIR),
        "note": "" if LLM_SETTINGS.enabled else "未配置 DEEPSEEK_API_KEY，当前为纯规则模式",
    }


@app.post("/api/llm/test")
def llm_test() -> dict:
    """一次最小调用验证 Key 是否可用，不消耗多少额度。"""
    if not LLM_SETTINGS.enabled:
        raise HTTPException(400, "未配置 DEEPSEEK_API_KEY")
    return DeepSeekClient(LLM_SETTINGS).ping()
