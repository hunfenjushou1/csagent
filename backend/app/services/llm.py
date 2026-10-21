"""DeepSeek 接入层（OpenAI 兼容协议）。

这一层只负责两件事：把 JSON 请求发出去、把结论缓存起来。
**判定逻辑一律不放在这里**——「模型找到了什么」和「这段依据能不能用」必须分离，
前者是模型的事，后者必须由代码说了算。混在一起，就没法回答"为什么这个值被拒绝"。

配置全部来自环境变量（或 backend/.env），不配也能跑，只是降级为纯规则模式。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# 提示词或校验规则一改，缓存必须整体失效，否则会拿旧结论喂新逻辑
PROMPT_VERSION = "align-v2"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def load_dotenv(path: Path) -> None:
    """极简 .env 解析，只为少一个 python-dotenv 依赖。

    已存在的环境变量优先，避免 .env 覆盖生产环境的真实配置。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class LLMSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = 90.0
    temperature: float = 0.0
    max_tokens: int = 4096
    cache_dir: Path | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls, root: Path) -> "LLMSettings":
        load_dotenv(root / ".env")
        try:
            timeout = float(os.getenv("DEEPSEEK_TIMEOUT") or 90)
        except ValueError:
            timeout = 90.0
        try:
            max_tokens = int(os.getenv("DEEPSEEK_MAX_TOKENS") or 4096)
        except ValueError:
            max_tokens = 4096
        return cls(
            api_key=(os.getenv("DEEPSEEK_API_KEY") or "").strip(),
            base_url=(os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            model=(os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip(),
            timeout=timeout,
            max_tokens=max_tokens,
            cache_dir=Path(os.getenv("LLM_CACHE_DIR") or (root / "storage" / ".llm_cache")),
        )


class LLMError(RuntimeError):
    """模型侧失败（网络 / 额度 / 返回不是 JSON）。

    调用方必须能捕获它并降级到规则模式——一次模型抖动不应该让整个任务失败。
    """


class _FatalError(LLMError):
    """重试无意义的错误（401 无效 Key、400 参数错、402 余额不足）。"""


def parse_json_object(text: str) -> dict[str, Any]:
    """模型偶尔会在 JSON 外面裹 ```json 或加一句解释，这里做容错。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回空内容")
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError(f"模型返回不是 JSON：{text[:120]}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回 JSON 解析失败：{text[:120]}") from exc
    if not isinstance(data, dict):
        raise LLMError("模型返回的 JSON 顶层不是对象")
    return data


class DeepSeekClient:
    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.calls = 0
        self.tokens = 0
        self.errors: list[str] = []

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def chat_json(self, system: str, user: str, max_tokens: int | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise LLMError("未配置 DEEPSEEK_API_KEY")

        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.temperature,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.settings.base_url}/chat/completions"

        last: Exception | None = None
        for attempt in range(3):
            try:
                return self._post(url, headers, payload)
            except _FatalError as exc:
                self.errors.append(str(exc))
                raise LLMError(str(exc)) from exc
            except LLMError as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
            except httpx.HTTPError as exc:
                last = LLMError(f"网络错误：{exc}")
                time.sleep(1.5 * (attempt + 1))

        self.errors.append(str(last))
        raise LLMError(str(last))

    def _post(self, url: str, headers: dict[str, str], payload: dict) -> dict[str, Any]:
        self.calls += 1
        try:
            with httpx.Client(timeout=self.settings.timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"网络错误：{exc}") from exc

        if resp.status_code >= 400:
            body = (resp.text or "")[:300]
            msg = f"HTTP {resp.status_code}：{body}"
            if resp.status_code == 429 or resp.status_code >= 500:
                raise LLMError(msg)          # 限流/抖动，值得重试
            raise _FatalError(msg)            # Key 错、参数错、余额不足，重试没用

        try:
            data = resp.json()
            choice = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            raise LLMError(f"响应结构异常：{resp.text[:200]}") from exc

        usage = data.get("usage") or {}
        self.tokens += int(usage.get("total_tokens") or 0)
        return parse_json_object(choice)

    def ping(self) -> dict[str, Any]:
        """给「测试连接」按钮用：一次最小调用，只验证 Key 和网络。"""
        started = time.time()
        try:
            data = self.chat_json(
                '你是连通性检查助手。只输出 JSON：{"ok":true}',
                "连通性测试，请返回 {\"ok\":true}",
                max_tokens=64,
            )
        except LLMError as exc:
            return {"ok": False, "error": str(exc), "model": self.settings.model}
        return {
            "ok": bool(data.get("ok", True)),
            "model": self.settings.model,
            "latency_ms": int((time.time() - started) * 1000),
        }


class ResultCache:
    """按「字段指纹 + 语料指纹」缓存模型结论。

    没有这一层，用户在审阅页每点一次「应用补充」都要重跑一遍模型：
    既费钱，也让同一份资料可能给出不同结果。有了它，第二次起完全离线。
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, Any] = {}
        self._dirty = False
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if raw.get("version") == PROMPT_VERSION:
                    self._items = raw.get("items") or {}
            except (json.JSONDecodeError, OSError):
                self._items = {}

    @staticmethod
    def make_key(*parts: str) -> str:
        h = hashlib.sha256(PROMPT_VERSION.encode("utf-8"))
        for part in parts:
            h.update(b"\x00")
            h.update((part or "").encode("utf-8"))
        return h.hexdigest()[:32]

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._items.get(key)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if self._items.get(key) == value:
                return
            self._items[key] = value
            self._dirty = True
            self.flush()

    def flush(self) -> None:
        if not self.path or not self._dirty:
            return
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(
                        {"version": PROMPT_VERSION, "items": self._items},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                tmp.replace(self.path)
                self._dirty = False
            except OSError:
                # 缓存写不进去不影响主流程，下次重算而已
                pass
