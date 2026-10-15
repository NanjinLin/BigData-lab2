"""Small server-side client for the OpenAI Responses API."""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelError(Exception):
    def __init__(self, message: str, code: int = 502) -> None:
        super().__init__(message)
        self.code = code


def normalize_base_url(value: str) -> str:
    """Accept a Responses-compatible HTTPS endpoint or local HTTP endpoint."""
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ModelError("模型服务 URL 必须是有效地址。", 400)
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password
            or any(character.isspace() for character in url)):
        raise ModelError("模型服务 URL 必须是 HTTP(S) 地址，且不能包含账号密码。", 400)
    if parsed.query or parsed.fragment or parsed.path.endswith("/responses"):
        raise ModelError("请填写 API 基础 URL（通常以 /v1 结尾），不要包含 /responses、查询参数或片段。", 400)
    if parsed.scheme == "http" and parsed.hostname.lower() not in ("localhost", "127.0.0.1", "::1"):
        raise ModelError("非本机模型服务请使用 HTTPS。", 400)
    try:
        parsed.port
    except ValueError as error:
        raise ModelError("模型服务 URL 的端口无效。", 400) from error
    return url


class ResponsesClient:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None, timeout: int = 45) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def create(self, *, input: str | list[dict], instructions: str,
               tools: list[dict] | None = None, tool_choice: str | dict | None = None,
               parallel_tool_calls: bool | None = None) -> dict:
        if not self.configured:
            raise ModelError("未配置 API Key（可在页面模型设置中填写，或使用 OPENAI_API_KEY）。", 503)
        payload: dict = {"model": self.model, "instructions": instructions,
                         "input": input, "store": False}
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        request = Request(
            self.base_url + "/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
        except HTTPError as error:
            try:
                parsed = json.loads(error.read(4096))
                detail = parsed.get("error", {}) if isinstance(parsed, dict) else {}
                reason = detail.get("code", "") if isinstance(detail, dict) else ""
            except (ValueError, UnicodeError):
                reason = ""
            safe_reason = reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", reason) else "unknown_error"
            if error.code in (400, 404):
                message = (f"模型服务返回 HTTP {error.code}（{safe_reason}）；"
                           f"请检查当前模型 {self.model[:100]} 是否受该服务支持。")
            else:
                message = f"模型服务返回 HTTP {error.code}（{safe_reason}）；请检查密钥、模型权限或额度。"
            raise ModelError(message, 503) from error
        except (URLError, TimeoutError, OSError) as error:
            raise ModelError("无法连接模型服务或等待超时；请检查网络与模型设置中的 API 基础 URL。", 503) from error
        if len(raw) > 2_000_000:
            raise ModelError("模型响应过大，已停止解析。")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise ModelError("模型服务没有返回有效 JSON。") from error
        if not isinstance(result, dict) or not isinstance(result.get("output"), list):
            raise ModelError("模型响应缺少 output 列表。")
        if result.get("status") not in (None, "completed"):
            raise ModelError(f"模型响应未完成：{result.get('status')}。")
        return result
