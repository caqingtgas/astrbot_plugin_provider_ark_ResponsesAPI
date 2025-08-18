# -*- coding: utf-8 -*-
# Volcengine Ark Responses API Provider for AstrBot
#
# 变更摘要（2025-08-18）：
# - 真·流式：SSE 增量分块输出（response.output_text.delta / response.completed）
# - tools 扁平化：{"type":"function","function":{...}}
#                 -> {"type":"function","name":...,"parameters":...,"description":...}
# - expire_at 支持“秒为 TTL”，并夹紧到 now+3d（_MAX_EXPIRATION_SECONDS），打印 UTC 到期时间
# - previous_response_id 失效：自动重建 +（可选）历史重放（上下文量由 AstrBot 决定）
# - 状态读写均在 skey_lock 内，避免竞态；异常捕获收敛；raw_completion 用 SimpleNamespace
# - ClientSession 在 __init__ 创建并复用；close() 统一释放（复用连接池）
#
# 参考：
# - PEP 8（导入风格/行长）：https://peps.python.org/pep-0008/             # noqa
# - aiohttp Session 生命周期与连接池：                                   # noqa
#   https://docs.aiohttp.org/en/stable/client_reference.html              # noqa
#   https://docs.aiohttp.org/en/stable/client_advanced.html               # noqa
# - OpenAI Responses 流式事件：                                           # noqa
#   https://platform.openai.com/docs/guides/streaming-responses           # noqa
#   https://openai.github.io/openai-agents-python/streaming/              # noqa
# - SSE 规范与示例：                                                      # noqa
#   https://www.w3.org/TR/eventsource/                                    # noqa
#   https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events  # noqa

import asyncio
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from types import SimpleNamespace

import aiohttp

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.entities import (
    LLMResponse,
    ProviderType,
    ToolCallsResult,
)


@dataclass
class _RespState:
    """每个 AstrBot session 的 Responses 状态"""
    last_id: str = ""          # 上一轮 response.id
    tools_sent: bool = False   # 是否已在首轮发送过 tools


@register_provider_adapter(
    "ark_responses",
    "Volcengine Ark (Responses API)",
    provider_type=ProviderType.CHAT_COMPLETION,
)
class ArkResponsesProvider(Provider):
    """
    - 非流式：POST /responses，支持 previous_response_id，自动重建与历史重放
    - 流式：POST /responses?stream=true（SSE），逐块输出 delta，完成时汇总 usage/id
    """

    _MAX_EXPIRATION_SECONDS = 3 * 24 * 3600  # 3 days

    # -------------------- 构造 & 基础 --------------------
    def __init__(
        self,
        provider_config: dict,
        provider_settings: dict,
        default_persona=None,
    ):
        super().__init__(provider_config, provider_settings, default_persona)

        # 基础配置
        self._keys: List[str] = [
            (k or "").strip()
            for k in provider_config.get("key", [])
            if (k or "").strip()
        ]
        self._key_idx: int = 0
        self._base: str = (
            provider_config.get("api_base")
            or "https://ark.cn-beijing.volces.com/api/v3"
        ).strip().rstrip("/")
        self._timeout: int = int(provider_config.get("timeout", 60))
        self._model_default: str = (
            provider_config.get("model")
            or provider_config.get("model_config", {}).get("model")
            or ""
        ).strip()
        self._auto_delete_on_reset: bool = bool(
            provider_config.get("auto_delete_on_reset", True)
        )
        self._max_retries: int = int(provider_config.get("max_retries", 3))
        self._retry_sleep: float = float(
            provider_config.get("retry_sleep", 0.2)
        )

        # 历史重放（默认**不限制条数**；上下文量由 AstrBot 控制）
        self._rehydrate_on_recreate: bool = bool(
            provider_config.get("rehydrate_on_recreate", True)
        )
        try:
            val = provider_config.get("rehydrate_max_messages", 0)
            self._rehydrate_max_messages: int = int(val)
        except (TypeError, ValueError):
            self._rehydrate_max_messages = 0
        self._rehydrate_strip_images: bool = bool(
            provider_config.get("rehydrate_strip_images", True)
        )

        # HTTP 会话（应用生命周期内复用）
        self._session: Optional[aiohttp.ClientSession] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout)
        )

        # 会话状态（本地持久化）
        data_dir: Path = StarTools.get_data_dir(
            "astrbot_plugin_provider_ark_responses"
        )
        # 修复：StarTools.get_data_dir 已返回 Path；直接使用 / 连接（pathlib 运算符）
        # 参考官方文档：pathlib Path 支持用 `/` 拼接子路径。&#8203;:contentReference[oaicite:3]{index=3}
        self._state_path: Path = data_dir / "responses_state.json"
        self._state_lock = asyncio.Lock()
        self._state_loaded = False
        self._state: Dict[str, _RespState] = {}

        # 每个 skey 的互斥锁，避免并发创建/重置竞态
        self._skey_locks: Dict[str, asyncio.Lock] = {}
        self._skey_dict_lock = asyncio.Lock()

        masked = (
            self._keys[0][:6] + "..." + self._keys[0][-4:]
            if self._keys else "EMPTY"
        )
        logger.info(
            "[ArkResponses] init base=%s, model=%s, key=%s, timeout=%s",
            self._base,
            self._model_default,
            masked,
            self._timeout,
        )

    # -------------------- AstrBot Provider 接口 --------------------
    def get_models(self) -> List[str]:
        m = self.provider_config.get("models")
        if isinstance(m, list) and m:
            return m
        return [self._model_default] if self._model_default else []

    def get_current_key(self) -> str:
        return self._keys[self._key_idx] if self._keys else ""

    def set_key(self, key: str):
        k = (key or "").strip()
        if k in self._keys:
            self._key_idx = self._keys.index(k)

    async def close(self):
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except (aiohttp.ClientError, RuntimeError) as e:
            logger.warning(
                "[ArkResponses] close session error (%s): %s",
                e.__class__.__name__,
                e,
            )

    # -------------------- 内部：状态持久化 --------------------
    async def _ensure_state_loaded(self):
        if self._state_loaded:
            return
        async with self._state_lock:
            if self._state_loaded:
                return
            try:
                if self._state_path.exists():
                    text = self._state_path.read_text(encoding="utf-8")
                    raw = json.loads(text)
                    for skey, v in (raw.get("map") or {}).items():
                        self._state[str(skey)] = _RespState(
                            last_id=str(v.get("last_id") or ""),
                            tools_sent=bool(v.get("tools_sent") or False),
                        )
                    if self._state:
                        logger.info(
                            "[ArkResponses] loaded %d response bindings "
                            "from %s",
                            len(self._state),
                            self._state_path,
                        )
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "[ArkResponses] load state failed (%s): %s",
                    e.__class__.__name__,
                    e,
                )
            finally:
                self._state_loaded = True

    async def _save_state(self):
        async with self._state_lock:
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "map": {
                        k: {"last_id": v.last_id, "tools_sent": v.tools_sent}
                        for k, v in self._state.items()
                    }
                }
                tmp = self._state_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self._state_path)
            except OSError as e:
                logger.warning(
                    "[ArkResponses] save state failed (%s): %s",
                    e.__class__.__name__,
                    e,
                )

    async def _get_skey_lock(self, skey: str) -> asyncio.Lock:
        async with self._skey_dict_lock:
            lock = self._skey_locks.get(skey)
            if lock is None:
                lock = asyncio.Lock()
                self._skey_locks[skey] = lock
            return lock

    # -------------------- 内部：HTTP --------------------
    async def _sess(self) -> aiohttp.ClientSession:
        # 统一复用；若在运行期被关闭则重建一次
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_current_key().strip()}",
            "Content-Type": "application/json",
        }

    async def _post_responses(
        self,
        payload: dict,
        *,
        stream: bool = False,
    ) -> Tuple[int, Union[dict, str, aiohttp.ClientResponse]]:
        url = f"{self._base}/responses"
        sess = await self._sess()
        if stream:
            resp = await sess.post(
                url, json=payload, headers=self._headers()
            )
            # 流式：返回响应对象，由调用方迭代
            return resp.status, resp
        async with sess.post(
            url, json=payload, headers=self._headers()
        ) as resp:
            ct = resp.content_type
            data: Union[dict, str]
            data = await (resp.json() if ct == "application/json"
                          else resp.text())
            return resp.status, data

    async def _delete_response(
        self, resp_id: str
    ) -> Tuple[int, Union[dict, str]]:
        if not resp_id:
            return 204, {}
        url = f"{self._base}/responses/{resp_id}"
        async with (await self._sess()).delete(
            url, headers=self._headers()
        ) as resp:
            ct = resp.content_type
            data: Union[dict, str]
            data = await (resp.json() if ct == "application/json"
                          else resp.text())
            return resp.status, data

    def _rotate_key(self):
        if not self._keys:
            return
        self._key_idx = (self._key_idx + 1) % len(self._keys)
        logger.warning(
            "[ArkResponses] rotate API key -> idx=%s", self._key_idx
        )

    # -------------------- 内部：请求构建 & 解析 --------------------
    def _model_config(self) -> Dict[str, Any]:
        cfg = dict(self.provider_config.get("model_config") or {})
        if self._model_default and not cfg.get("model"):
            cfg["model"] = self._model_default
        return cfg

    def _ensure_model(self, model: Optional[str]) -> str:
        m = (model or self.get_model() or self._model_default).strip()
        if not m:
            raise RuntimeError("ArkResponses: model not configured")
        return m

    def _make_user_message(self, text: str) -> Dict[str, Any]:
        return {"role": "user", "content": str(text)}

    def _make_system_message(self, text: str) -> Dict[str, Any]:
        return {"role": "system", "content": str(text)}

    def _contexts_to_ark_messages(
        self,
        contexts: Optional[List[dict]],
    ) -> List[dict]:
        """AstrBot contexts -> Ark 文本消息（0=不限量）。"""
        if not contexts:
            return []

        def _to_text(c):
            v = c.get("content")
            if isinstance(v, list):
                parts = []
                for item in v:
                    if isinstance(item, dict):
                        if "text" in item:
                            parts.append(str(item.get("text") or ""))
                        elif (not self._rehydrate_strip_images
                              and "image_url" in item):
                            # 如需重放图片，可在此扩展 Ark 图文结构；默认忽略图片片段
                            pass
                    else:
                        parts.append(str(item))
                return "".join(parts).strip()
            return ("" if v is None else str(v)).strip()

        filtered = []
        for msg in contexts:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").lower()
            if role not in ("system", "user", "assistant"):
                continue
            text = _to_text(msg)
            if text:
                filtered.append({"role": role, "content": text})

        # 简化：__init__ 已保证为 int
        if self._rehydrate_max_messages > 0:
            n = self._rehydrate_max_messages
            if len(filtered) > n:
                filtered = filtered[-n:]
        return filtered

    def _openai_msgs_to_function_outputs(
        self,
        tcr_msgs: List[dict],
    ) -> List[dict]:
        outputs: List[dict] = []
        for msg in tcr_msgs:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            call_id = msg.get("tool_call_id") or msg.get("id") or ""
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(
                    [c.get("text", "") if isinstance(c, dict) else str(c)
                     for c in content]
                )
            outputs.append({
                "type": "function_call_output",
                "call_id": str(call_id),
                "output": "" if content is None else str(content),
                "status": "completed",
            })
        return outputs

    def _build_input_array(
        self,
        *,
        first_round: bool,
        prompt: Optional[str],
        system_prompt: Optional[str],
        tool_calls_result: Optional[
            Union[ToolCallsResult, List[ToolCallsResult], None]
        ],
        rehydrate_messages: Optional[List[dict]] = None,
    ) -> List[dict]:
        items: List[dict] = []
        if first_round and system_prompt:
            items.append(self._make_system_message(system_prompt))
        if first_round and rehydrate_messages:
            items.extend(rehydrate_messages)

        if tool_calls_result:
            if isinstance(tool_calls_result, ToolCallsResult):
                msgs = tool_calls_result.to_openai_messages()
                items.extend(self._openai_msgs_to_function_outputs(msgs))
            else:
                for t in tool_calls_result:
                    msgs = t.to_openai_messages()
                    items.extend(self._openai_msgs_to_function_outputs(msgs))

        if prompt:
            items.append(self._make_user_message(prompt))
        if not items:
            items.append(self._make_user_message(""))
        return items

    def _convert_openai_tools_to_ark(
        self,
        openai_tools: List[dict],
    ) -> List[dict]:
        """OpenAI 风格 tools -> Ark 扁平结构"""
        ark_tools: List[dict] = []
        for t in (openai_tools or []):
            if not isinstance(t, dict) or t.get("type") != "function":
                continue
            f = t.get("function") or {}
            ark_tools.append({
                "type": "function",
                "name": f.get("name", ""),
                "parameters": f.get("parameters") or {},
                "description": f.get("description", ""),
            })
        return ark_tools

    def _attach_tools_if_first_round(self, payload: dict, tools) -> None:
        if not tools:
            return
        openai_style = tools.get_func_desc_openai_style(
            omit_empty_parameter_field=False
        )
        ark_style = self._convert_openai_tools_to_ark(openai_style)
        if ark_style:
            payload["tools"] = ark_style

    def _normalize_expire_at(self, cfg: Dict[str, Any]) -> None:
        """expire_at 支持秒 TTL（<=3d）或 epoch（>3d），并对齐最大 3 天。"""
        if "expire_at" not in cfg:
            return
        val = cfg.get("expire_at")
        try:
            sec = float(val)
        except (TypeError, ValueError):
            logger.warning(
                "[ArkResponses] expire_at is not numeric, ignore: %r", val
            )
            return

        now = int(time.time())
        max_span = self._MAX_EXPIRATION_SECONDS
        if sec <= max_span:
            ttl = int(max(0, sec))
            ts = now + ttl
            cfg["expire_at"] = ts
            logger.info(
                "[ArkResponses] expire_at: TTL=%ss -> epoch=%s, utc=%s "
                "(max=3d)",
                ttl,
                ts,
                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            )
        else:
            ts = int(sec)
            max_ts = now + max_span
            if ts > max_ts:
                logger.warning(
                    "[ArkResponses] expire_at epoch (%s) exceeds max "
                    "(now+3d=%s); clamping to max.",
                    ts,
                    max_ts,
                )
                ts = max_ts
                cfg["expire_at"] = ts
            logger.info(
                "[ArkResponses] expire_at: epoch=%s, utc=%s",
                ts,
                datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            )

    def _parse_usage(self, data: dict) -> Dict[str, Any]:
        usage = data.get("usage") or {}

        def _to_int(v, default=0) -> int:
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        prompt_tokens = _to_int(
            usage.get("input_tokens") or usage.get("prompt_tokens")
        )
        completion_tokens = _to_int(
            usage.get("output_tokens") or usage.get("completion_tokens")
        )
        total_tokens_field = usage.get("total_tokens")
        total = (_to_int(total_tokens_field, prompt_tokens + completion_tokens)
                 if total_tokens_field is not None
                 else (prompt_tokens + completion_tokens))

        cached_tokens = 0
        try:
            it_details = (
                usage.get("input_tokens_details")
                or usage.get("prompt_tokens_details")
                or {}
            )
            cached_tokens = _to_int(it_details.get("cached_tokens"), 0)
        except (AttributeError, TypeError, ValueError):
            cached_tokens = 0

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total,
            "cached_tokens": cached_tokens,
            "raw": usage,
        }

    def _parse_outputs(self, data: dict) -> Tuple[Optional[str], List[dict]]:
        text_parts: List[str] = []
        fcs: List[dict] = []
        for item in (data.get("output") or []):
            typ = item.get("type")
            if typ == "message":
                content = item.get("content") or []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        text_parts.append(str(c.get("text") or ""))
            elif typ == "function_call":
                name = item.get("name") or ""
                call_id = item.get("call_id") or ""
                args_raw = item.get("arguments") or "{}"
                try:
                    args = (json.loads(args_raw) if isinstance(args_raw, str)
                            else dict(args_raw))
                except (ValueError, TypeError):
                    args = {}
                fcs.append({"name": name, "arguments": args, "call_id": call_id})
        assistant_text = "".join(text_parts) if text_parts else None
        return assistant_text, fcs

    def _build_llm_response(
        self,
        *,
        assistant_text: Optional[str],
        function_calls: List[dict],
        usage_info: Dict[str, Any],
        response_id: str,
        previous_response_id: Optional[str],
    ) -> LLMResponse:
        if function_calls:
            llm = LLMResponse(role="tool")
            llm.tools_call_name = [fc["name"] for fc in function_calls]
            llm.tools_call_args = [fc["arguments"] for fc in function_calls]
            llm.tools_call_ids = [fc["call_id"] for fc in function_calls]
        else:
            if assistant_text is None or str(assistant_text).strip() == "":
                raise RuntimeError(
                    "ArkResponses: completion is empty and no function calls"
                )
            llm = LLMResponse(role="assistant", completion_text=str(assistant_text))

        llm.raw_completion = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=usage_info.get("prompt_tokens", 0),
                completion_tokens=usage_info.get("completion_tokens", 0),
                total_tokens=usage_info.get("total_tokens", 0),
            )
        )
        llm.extra = {
            "ark_usage": usage_info.get("raw", {}),
            "ark_cached_tokens": usage_info.get("cached_tokens", 0),
            "ark_response_id": response_id,
            "ark_previous_response_id": previous_response_id,
        }

        logger.info(
            "[ArkResponses] usage: input=%s, output=%s, total=%s, "
            "cached_tokens=%s, prev_id=%s, resp_id=%s",
            usage_info.get("prompt_tokens", 0),
            usage_info.get("completion_tokens", 0),
            usage_info.get("total_tokens", 0),
            usage_info.get("cached_tokens", 0),
            previous_response_id,
            response_id,
        )
        return llm

    # -------------------- 错误识别 --------------------
    def _is_prev_id_invalid(
        self,
        http_status: int,
        data: Union[dict, str],
    ) -> bool:
        if http_status == 404:
            return True
        if http_status in (400, 422):
            s = (json.dumps(data, ensure_ascii=False)
                 if isinstance(data, dict) else str(data))
            s = s.lower()
            keywords = [
                "previous_response_id",
                "invalid",
                "expired",
                "not found",
                "does not exist",
                "deleted",
            ]
            return any(k in s for k in keywords)
        return False

    # -------------------- 状态（锁内） --------------------
    async def _handle_session_state_under_lock(
        self,
        skey: str,
        contexts: Optional[List[dict]],
    ) -> Tuple[_RespState, bool, Optional[str], bool]:
        """/reset、复用或新建。返回 (state, first_round, previous_id, first_call_after_reset)"""
        state = self._state.get(skey) or _RespState()
        first_call_after_reset = False

        if not contexts:  # None / [] -> False
            if state.last_id:
                old = state.last_id
                if self._auto_delete_on_reset:
                    try:
                        st, _ = await self._delete_response(old)
                        logger.info(
                            "[ArkResponses] reset -> delete old response_id=%s "
                            "(status=%s)",
                            old,
                            st,
                        )
                    except (aiohttp.ClientError, RuntimeError) as e:
                        logger.warning(
                            "[ArkResponses] reset delete failed for %s: %s",
                            old,
                            e,
                        )
            state = _RespState()
            self._state[skey] = state
            await self._save_state()
            first_call_after_reset = True

        first_round = first_call_after_reset or (not state.last_id)
        previous_id = state.last_id if not first_round else None
        return state, first_round, previous_id, first_call_after_reset

    # -------------------- 非流式：调用 & 重试 --------------------
    async def _execute_api_call_with_retries(
        self,
        *,
        base_payload: Dict[str, Any],
        contexts: Optional[List[dict]],
        func_tool,
        system_prompt: Optional[str],
        prompt: Optional[str],
        tool_calls_result: Optional[Union[ToolCallsResult, List[ToolCallsResult]]],
        first_round: bool,
        state: _RespState,
        skey: str,
    ) -> Tuple[dict, bool, Optional[str]]:
        """
        执行带重试的 API 调用（非流式）。
        返回：(data, rehydrated, previous_id_used)
        """
        payload = dict(base_payload)  # 浅拷贝
        previous_id_used = payload.get("previous_response_id")
        last_err: Optional[BaseException] = None

        for attempt in range(self._max_retries):
            if attempt > 0:
                await asyncio.sleep(self._retry_sleep * attempt)

            try:
                status, data = await self._post_responses(
                    payload, stream=False
                )

                # previous_response_id 失效：重建 +（可选）历史重放
                if self._is_prev_id_invalid(status, data):
                    logger.warning(
                        "[ArkResponses] previous_response_id invalid -> "
                        "recreate session and retry"
                    )
                    state.last_id = ""
                    state.tools_sent = False
                    self._state[skey] = state
                    await self._save_state()

                    rehydrate_msgs: List[dict] = []
                    if self._rehydrate_on_recreate and contexts:
                        rehydrate_msgs = self._contexts_to_ark_messages(contexts)
                        logger.info(
                            "[ArkResponses] rehydrate on recreate: %d messages "
                            "injected",
                            len(rehydrate_msgs),
                        )

                    payload.pop("previous_response_id", None)
                    payload.pop("tools", None)
                    if func_tool:
                        self._attach_tools_if_first_round(payload, func_tool)

                    payload["input"] = self._build_input_array(
                        first_round=True,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        tool_calls_result=tool_calls_result,
                        rehydrate_messages=rehydrate_msgs or None,
                    )
                    status, data = await self._post_responses(
                        payload, stream=False
                    )
                    if not (isinstance(data, dict) and status < 400):
                        raise RuntimeError(
                            f"ArkResponses error http={status}, data={data}"
                        )
                    return data, bool(rehydrate_msgs), previous_id_used

                if not (isinstance(data, dict) and status < 400):
                    raise RuntimeError(
                        f"ArkResponses error http={status}, data={data}"
                    )

                return data, False, previous_id_used

            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                last_err = e
                msg = str(e)
                # 429 换 key
                if "429" in msg or "Too Many Requests" in msg:
                    if len(self._keys) > 1:
                        self._rotate_key()
                        continue
                # 5xx 重试（通过错误消息前缀/标记判断）
                if "http=5" in msg or " 5" in msg[:8]:
                    continue
                if attempt == self._max_retries - 1:
                    logger.error(
                        "[ArkResponses] request failed after retries: %s", e
                    )
                    raise

        if last_err:
            raise last_err
        raise RuntimeError("ArkResponses: unknown error")

    # -------------------- SSE 迭代工具（流式） --------------------
    async def _iterate_sse(self, resp: aiohttp.ClientResponse):
        """
        解析 SSE 帧，yield (event, data_str)
        兼容两种格式：
          1) 标准 SSE：含 'event:' 与 'data:'
          2) 仅 'data:'，其中 JSON 自带 'type' 字段
        """
        buffer = b""
        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            buffer += chunk
            while b"\n\n" in buffer:
                block, buffer = buffer.split(b"\n\n", 1)
                try:
                    lines = block.decode("utf-8", errors="strict").splitlines()
                except UnicodeDecodeError as e:
                    logger.warning(
                        "[ArkResponses] SSE decode warning (%s): %s",
                        e.__class__.__name__,
                        e,
                    )
                    continue
                event_type = None
                data_lines = []
                for line in lines:
                    s = line.strip()
                    if s.startswith("event:"):
                        event_type = s[6:].strip()
                    elif s.startswith("data:"):
                        data_lines.append(s[5:].lstrip())
                data_str = "\n".join(data_lines).strip()
                yield event_type, data_str

    # -------------------- Provider 主流程（非流式） --------------------
    async def text_chat(
        self,
        prompt: str,
        session_id: str = None,
        image_urls: List[str] = None,
        func_tool=None,
        contexts: List[dict] = None,
        system_prompt: str = None,
        tool_calls_result: Union[ToolCallsResult, List[ToolCallsResult], None] = None,
        model: str | None = None,
        **kwargs,
    ) -> LLMResponse:

        await self._ensure_state_loaded()

        if not self.get_current_key():
            raise RuntimeError("ArkResponses: API Key not configured")

        model_name = self._ensure_model(model)
        skey = (session_id or "global").strip()

        skey_lock = await self._get_skey_lock(skey)
        async with skey_lock:
            state, first_round, previous_id, _ = (
                await self._handle_session_state_under_lock(skey, contexts)
            )

            # 基础 payload
            cfg = self._model_config()
            self._normalize_expire_at(cfg)
            payload: Dict[str, Any] = {
                "model": model_name,
                "input": [],
                "stream": False,
            }
            if previous_id:
                payload["previous_response_id"] = previous_id
            for k, v in cfg.items():
                if k != "model":
                    payload[k] = v
            if first_round and not state.tools_sent and func_tool:
                self._attach_tools_if_first_round(payload, func_tool)
            payload["input"] = self._build_input_array(
                first_round=first_round,
                prompt=prompt,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                rehydrate_messages=None,
            )

            data, rehydrated, prev_used = await self._execute_api_call_with_retries(
                base_payload=payload,
                contexts=contexts,
                func_tool=func_tool,
                system_prompt=system_prompt,
                prompt=prompt,
                tool_calls_result=tool_calls_result,
                first_round=first_round,
                state=state,
                skey=skey,
            )

            usage = self._parse_usage(data)
            assistant_text, fcs = self._parse_outputs(data)
            new_id = str(data.get("id") or "")

            # 更新状态
            if new_id:
                state.last_id = new_id
            if (first_round or rehydrated) and ("tools" in payload):
                state.tools_sent = True
            self._state[skey] = state
            await self._save_state()

            return self._build_llm_response(
                assistant_text=assistant_text,
                function_calls=fcs,
                usage_info=usage,
                response_id=new_id,
                previous_response_id=prev_used,
            )

    # -------------------- Provider 主流程（流式） --------------------
    async def text_chat_stream(
        self,
        prompt: str,
        session_id: str = None,
        image_urls: List[str] = None,
        func_tool=None,
        contexts: List[dict] = None,
        system_prompt: str = None,
        tool_calls_result: Union[ToolCallsResult, List[ToolCallsResult], None] = None,
        model: str | None = None,
        **kwargs,
    ):
        """
        真·流式：逐块输出 delta，尾部输出一个“收尾块”（不重复文本，只带 usage/id）
        事件模型：response.output_text.delta / response.completed
        """
        await self._ensure_state_loaded()

        if not self.get_current_key():
            raise RuntimeError("ArkResponses: API Key not configured")

        model_name = self._ensure_model(model)
        skey = (session_id or "global").strip()

        skey_lock = await self._get_skey_lock(skey)
        async with skey_lock:
            state, first_round, previous_id, _ = (
                await self._handle_session_state_under_lock(skey, contexts)
            )

            cfg = self._model_config()
            self._normalize_expire_at(cfg)
            payload: Dict[str, Any] = {
                "model": model_name,
                "input": [],
                "stream": True,
            }
            if previous_id:
                payload["previous_response_id"] = previous_id
            for k, v in cfg.items():
                if k != "model":
                    payload[k] = v
            if first_round and not state.tools_sent and func_tool:
                self._attach_tools_if_first_round(payload, func_tool)
            payload["input"] = self._build_input_array(
                first_round=first_round,
                prompt=prompt,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                rehydrate_messages=None,
            )

            # 发起流式请求
            status, resp_obj = await self._post_responses(
                payload, stream=True
            )
            if not isinstance(resp_obj, aiohttp.ClientResponse):
                raise RuntimeError(
                    f"ArkResponses stream error http={status}, data={resp_obj}"
                )
            resp = resp_obj

            if status >= 400:
                try:
                    err = await resp.json()
                except json.JSONDecodeError as e:
                    logger.warning(
                        "[ArkResponses] stream error JSON decode (%s): %s",
                        e.__class__.__name__,
                        e,
                    )
                    err = await resp.text()
                if self._is_prev_id_invalid(status, err):
                    logger.warning(
                        "[ArkResponses] previous_response_id invalid (stream) "
                        "-> recreate session and retry"
                    )
                    state.last_id = ""
                    state.tools_sent = False
                    self._state[skey] = state
                    await self._save_state()

                    rehydrate_msgs: List[dict] = []
                    if self._rehydrate_on_recreate and contexts:
                        rehydrate_msgs = self._contexts_to_ark_messages(contexts)
                        logger.info(
                            "[ArkResponses] rehydrate on recreate (stream): "
                            "%d messages injected",
                            len(rehydrate_msgs),
                        )

                    payload.pop("previous_response_id", None)
                    payload.pop("tools", None)
                    if func_tool:
                        self._attach_tools_if_first_round(payload, func_tool)
                    payload["input"] = self._build_input_array(
                        first_round=True,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        tool_calls_result=tool_calls_result,
                        rehydrate_messages=rehydrate_msgs or None,
                    )

                    status2, resp_obj2 = await self._post_responses(
                        payload, stream=True
                    )
                    if not isinstance(resp_obj2, aiohttp.ClientResponse):
                        raise RuntimeError(
                            f"ArkResponses stream error http={status2}, "
                            f"data={resp_obj2}"
                        )
                    resp = resp_obj2
                    if status2 >= 400:
                        try:
                            err2 = await resp.json()
                        except json.JSONDecodeError:
                            err2 = await resp.text()
                        raise RuntimeError(
                            f"ArkResponses stream error http={status2}, "
                            f"data={err2}"
                        )
                else:
                    raise RuntimeError(
                        f"ArkResponses stream error http={status}, data={err}"
                    )

            # 正常流
            new_id: str = ""
            prev_used = previous_id
            usage_final: Dict[str, Any] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "raw": {},
            }

            try:
                async for event, data_str in self._iterate_sse(resp):
                    if not data_str:
                        continue
                    if data_str == "[DONE]":
                        break

                    try:
                        obj = json.loads(data_str)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "[ArkResponses] stream JSON decode warning (%s): %s",
                            e.__class__.__name__,
                            e,
                        )
                        continue

                    evt = (event or obj.get("type") or "").strip()

                    if (evt.endswith("response.output_text.delta")
                            or evt.endswith("output_text.delta")):
                        delta = obj.get("delta") or obj.get("text") or ""
                        if delta:
                            chunk = LLMResponse(
                                role="assistant",
                                completion_text=str(delta),
                            )
                            chunk.is_chunk = True
                            yield chunk
                        continue

                    if (evt.endswith("response.completed")
                            or evt.endswith("response.complete")
                            or evt.endswith("response.done")):
                        response_obj = obj.get("response") or obj
                        if isinstance(response_obj, dict):
                            usage_final = self._parse_usage(response_obj)
                            new_id = str(response_obj.get("id") or new_id)
                        continue

                    if (evt.endswith("response.failed")
                            or evt.endswith("response.incomplete")
                            or evt.endswith("response.error")):
                        msg = (
                            obj.get("error", {}).get("message")
                            or obj.get("message")
                            or data_str
                        )
                        raise RuntimeError(f"ArkResponses stream failed: {msg}")

            finally:
                if not resp.content.at_eof():
                    try:
                        await resp.release()
                    except aiohttp.ClientError as e:
                        logger.exception(
                            "[ArkResponses] stream release error (%s): %s",
                            e.__class__.__name__,
                            e,
                        )

            # 更新状态（以完成事件中的 id 为准）
            if new_id:
                state.last_id = new_id
            if (first_round or ("tools" in payload)) and (not state.tools_sent):
                state.tools_sent = True
            self._state[skey] = state
            await self._save_state()

            # “收尾块”（不重复文本，只携 usage/ids，便于统计）
            tail = LLMResponse(role="assistant", completion_text="")
            tail.is_chunk = False
            tail.raw_completion = SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=usage_final.get("prompt_tokens", 0),
                    completion_tokens=usage_final.get("completion_tokens", 0),
                    total_tokens=usage_final.get("total_tokens", 0),
                )
            )
            tail.extra = {
                "ark_usage": usage_final.get("raw", {}),
                "ark_cached_tokens": usage_final.get("cached_tokens", 0),
                "ark_response_id": new_id,
                "ark_previous_response_id": prev_used,
            }
            yield tail
