# -*- coding: utf-8 -*-
# Volcengine Ark Responses API Provider for AstrBot
#
# 变更摘要（2025-08-18）：
# - tools 扁平化：{"type":"function","function":{...}} -> {"type":"function","name":...,"parameters":...,"description":...}
# - expire_at 支持“秒为 TTL”，并夹紧到 now+3d，日志打印 UTC 到期时间
# - 过期自动重建 + 历史重放（rehydrate）：默认“**不限制消息条数**”（rehydrate_max_messages=0）
# - 修正 _ensure_state_loaded() 早期稿的拼写问题
#
# 依赖：aiohttp

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import aiohttp

from astrbot import logger
from astrbot.api.star import StarTools
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.entities import LLMResponse, ProviderType, ToolCallsResult


@dataclass
class _RespState:
    """记录每个 AstrBot session 的 Responses 会话状态"""
    last_id: str = ""         # 上一轮 response.id
    tools_sent: bool = False  # 是否已在首轮发送过 tools


@register_provider_adapter(
    "ark_responses",
    "Volcengine Ark (Responses API)",
    provider_type=ProviderType.CHAT_COMPLETION,
)
class ArkResponsesProvider(Provider):
    """
    非流式稳定实现：
    - POST /responses（非流式）
    - 自动管理 previous_response_id（创建/复用/失效自动重建）
    - 解析工具调用并转换为 AstrBot 的 LLMResponse(tool)
    - 打印 usage & cached_tokens
    """

    # -------------- 构造 & 基础 --------------
    def __init__(self, provider_config: dict, provider_settings: dict, default_persona=None):
        super().__init__(provider_config, provider_settings, default_persona)

        # 基础配置
        self._keys: List[str] = [(k or "").strip() for k in provider_config.get("key", []) if (k or "").strip()]
        self._key_idx: int = 0
        self._base: str = (provider_config.get("api_base") or "https://ark.cn-beijing.volces.com/api/v3").strip().rstrip("/")
        self._timeout: int = int(provider_config.get("timeout", 60))
        self._model_default: str = (
            provider_config.get("model") or
            provider_config.get("model_config", {}).get("model") or
            ""
        ).strip()
        self._auto_delete_on_reset: bool = bool(provider_config.get("auto_delete_on_reset", True))
        self._max_retries: int = int(provider_config.get("max_retries", 3))
        self._retry_sleep: float = float(provider_config.get("retry_sleep", 0.2))

        # 重放历史（默认**不限制条数**）
        self._rehydrate_on_recreate: bool = bool(provider_config.get("rehydrate_on_recreate", True))
        # 0 表示不限量；>0 表示只重放最近 N 条
        try:
            self._rehydrate_max_messages: int = int(provider_config.get("rehydrate_max_messages", 0))
        except Exception:
            self._rehydrate_max_messages = 0
        self._rehydrate_strip_images: bool = bool(provider_config.get("rehydrate_strip_images", True))

        # HTTP 会话
        self._session: Optional[aiohttp.ClientSession] = None

        # 会话状态（持久化）
        data_dir = StarTools.get_data_dir("astrbot_plugin_provider_ark_responses")
        self._state_path: Path = Path(data_dir) / "responses_state.json"
        self._state_lock = asyncio.Lock()
        self._state_loaded = False
        self._state: Dict[str, _RespState] = {}

        # 每个 skey 的互斥锁，避免并发创建/重置竞态
        self._skey_locks: Dict[str, asyncio.Lock] = {}
        self._skey_dict_lock = asyncio.Lock()

        masked = (self._keys[0][:6] + "..." + self._keys[0][-4:]) if self._keys else "EMPTY"
        logger.info(
            "[ArkResponses] init base=%s, model=%s, key=%s, timeout=%s",
            self._base, self._model_default, masked, self._timeout
        )

    # -------------- AstrBot Provider 接口 --------------
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
        except Exception as e:
            logger.warning("[ArkResponses] close session error: %s", e)

    # -------------- 内部：状态持久化 --------------
    async def _ensure_state_loaded(self):
        if self._state_loaded:
            return
        async with self._state_lock:
            if self._state_loaded:
                return
            try:
                if self._state_path.exists():
                    raw = json.loads(self._state_path.read_text(encoding="utf-8"))
                    for skey, v in (raw.get("map") or {}).items():
                        self._state[str(skey)] = _RespState(
                            last_id=str(v.get("last_id") or ""),
                            tools_sent=bool(v.get("tools_sent") or False),
                        )
                    if self._state:
                        logger.info("[ArkResponses] loaded %d response bindings from %s", len(self._state), self._state_path)
            except Exception as e:
                logger.warning("[ArkResponses] load state failed: %s", e)
            finally:
                self._state_loaded = True

    async def _save_state(self):
        async with self._state_lock:
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                data = {"map": {k: {"last_id": v.last_id, "tools_sent": v.tools_sent} for k, v in self._state.items()}}
                tmp = self._state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self._state_path)
            except Exception as e:
                logger.warning("[ArkResponses] save state failed: %s", e)

    async def _get_skey_lock(self, skey: str) -> asyncio.Lock:
        async with self._skey_dict_lock:
            lock = self._skey_locks.get(skey)
            if lock is None:
                lock = asyncio.Lock()
                self._skey_locks[skey] = lock
            return lock

    # -------------- 内部：HTTP --------------
    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self._session

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_current_key().strip()}",
            "Content-Type": "application/json",
        }

    async def _post_responses(self, payload: dict) -> Tuple[int, Union[dict, str]]:
        url = f"{self._base}/responses"
        async with (await self._sess()).post(url, json=payload, headers=self._headers()) as resp:
            ct = resp.content_type
            data: Union[dict, str]
            data = await (resp.json() if ct == "application/json" else resp.text())
            return resp.status, data

    async def _delete_response(self, resp_id: str) -> Tuple[int, Union[dict, str]]:
        if not resp_id:
            return 204, {}
        url = f"{self._base}/responses/{resp_id}"
        async with (await self._sess()).delete(url, headers=self._headers()) as resp:
            ct = resp.content_type
            data: Union[dict, str]
            data = await (resp.json() if ct == "application/json" else resp.text())
            return resp.status, data

    def _rotate_key(self):
        if not self._keys:
            return
        self._key_idx = (self._key_idx + 1) % len(self._keys)
        logger.warning("[ArkResponses] rotate API key -> idx=%s", self._key_idx)

    # -------------- 内部：请求构建 --------------
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

    # === 将 AstrBot contexts 重放为 Ark 支持的消息（纯文本；0=不限量） ===
    def _contexts_to_ark_messages(self, contexts: Optional[List[dict]]) -> List[dict]:
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
                        elif not self._rehydrate_strip_images and "image_url" in item:
                            # 如需重放图片，可在此扩展 Ark 的图文结构；默认忽略图片片段
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

        # 仅当配置了 >0 时才裁剪；0 表示不限量
        if isinstance(self._rehydrate_max_messages, int) and self._rehydrate_max_messages > 0:
            n = self._rehydrate_max_messages
            if len(filtered) > n:
                filtered = filtered[-n:]
        return filtered

    def _openai_msgs_to_function_outputs(self, tcr_msgs: List[dict]) -> List[dict]:
        outputs: List[dict] = []
        for msg in tcr_msgs:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "tool":
                continue
            call_id = msg.get("tool_call_id") or msg.get("id") or ""
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join([c.get("text", "") if isinstance(c, dict) else str(c) for c in content])
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": str(call_id),
                    "output": "" if content is None else str(content),
                    "status": "completed",
                }
            )
        return outputs

    def _build_input_array(
        self,
        *,
        first_round: bool,
        prompt: Optional[str],
        system_prompt: Optional[str],
        tool_calls_result: Optional[Union[ToolCallsResult, List[ToolCallsResult]]],
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

    # === OpenAI 风格 tools -> Ark 扁平结构 ===
    def _convert_openai_tools_to_ark(self, openai_tools: List[dict]) -> List[dict]:
        ark_tools: List[dict] = []
        for t in (openai_tools or []):
            if not isinstance(t, dict):
                continue
            if t.get("type") != "function":
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
        openai_style = tools.get_func_desc_openai_style(omit_empty_parameter_field=False)
        ark_style = self._convert_openai_tools_to_ark(openai_style)
        if ark_style:
            payload["tools"] = ark_style

    # === expire_at 归一化（支持 TTL 语义 + 最大 3 天） ===
    def _normalize_expire_at(self, cfg: Dict[str, Any]) -> None:
        if "expire_at" not in cfg:
            return
        val = cfg.get("expire_at")
        try:
            sec = float(val)
        except Exception:
            logger.warning("[ArkResponses] expire_at is not numeric, ignore: %r", val)
            return

        now = int(time.time())
        MAX = 3 * 24 * 3600  # 3 days

        if sec <= MAX:
            ttl = int(max(0, sec))
            ts = now + ttl
            cfg["expire_at"] = ts
            logger.info(
                "[ArkResponses] expire_at: TTL=%ss -> epoch=%s, utc=%s (max=3d)",
                ttl, ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            )
        else:
            ts = int(sec)
            max_ts = now + MAX
            if ts > max_ts:
                logger.warning(
                    "[ArkResponses] expire_at epoch (%s) exceeds max (now+3d=%s); clamping to max.",
                    ts, max_ts
                )
                ts = max_ts
                cfg["expire_at"] = ts
            logger.info(
                "[ArkResponses] expire_at: epoch=%s, utc=%s",
                ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            )

    # -------------- 内部：响应解析 --------------
    def _parse_usage(self, data: dict) -> Dict[str, Any]:
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total_tokens = usage.get("total_tokens")
        try:
            total = int(total_tokens) if total_tokens is not None else prompt_tokens + completion_tokens
        except (ValueError, TypeError):
            total = prompt_tokens + completion_tokens

        cached_tokens = 0
        try:
            it_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
            cached_tokens = int(it_details.get("cached_tokens") or 0)
        except Exception:
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
                    args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                except Exception:
                    args = {}
                fcs.append({"name": name, "arguments": args, "call_id": call_id})
        assistant_text = "".join(text_parts) if text_parts else None
        return assistant_text, fcs

    def _build_llm_response(
        self, *,
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
                raise RuntimeError("ArkResponses: completion is empty and no function calls")
            llm = LLMResponse(role="assistant", completion_text=str(assistant_text))

        class _Raw: ...
        raw = _Raw()
        raw.usage = _Raw()
        raw.usage.prompt_tokens = usage_info["prompt_tokens"]
        raw.usage.completion_tokens = usage_info["completion_tokens"]
        raw.usage.total_tokens = usage_info["total_tokens"]
        llm.raw_completion = raw

        try:
            llm.extra = {
                "ark_usage": usage_info["raw"],
                "ark_cached_tokens": usage_info["cached_tokens"],
                "ark_response_id": response_id,
                "ark_previous_response_id": previous_response_id,
            }
        except Exception:
            pass

        logger.info(
            "[ArkResponses] usage: input=%s, output=%s, total=%s, cached_tokens=%s, prev_id=%s, resp_id=%s",
            usage_info["prompt_tokens"], usage_info["completion_tokens"], usage_info["total_tokens"],
            usage_info["cached_tokens"], previous_response_id, response_id
        )
        return llm

    # -------------- 内部：错误恢复 --------------
    def _is_prev_id_invalid(self, http_status: int, data: Union[dict, str]) -> bool:
        if http_status == 404:
            return True
        if http_status in (400, 422):
            s = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
            s = s.lower()
            keywords = ["previous_response_id", "invalid", "expired", "not found", "does not exist", "deleted"]
            return any(k in s for k in keywords)
        return False

    # -------------- Provider 主流程 --------------
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

        # /reset：AstrBot 清空 contexts 时，主动丢弃已记录的 response_id
        first_call_after_reset = False
        state_before = self._state.get(skey)
        if (contexts is None) or (isinstance(contexts, list) and len(contexts) == 0):
            if state_before and state_before.last_id:
                old = state_before.last_id
                if self._auto_delete_on_reset:
                    try:
                        st, _ = await self._delete_response(old)
                        logger.info("[ArkResponses] reset -> delete old response_id=%s (status=%s)", old, st)
                    except Exception as e:
                        logger.warning("[ArkResponses] reset delete failed for %s: %s", old, e)
                self._state.pop(skey, None)
                await self._save_state()
            first_call_after_reset = True

        # skey 互斥，避免并发创建
        skey_lock = await self._get_skey_lock(skey)
        async with skey_lock:
            state = self._state.get(skey) or _RespState()
            first_round = first_call_after_reset or (not state.last_id)

            # 可能的重放消息（只有在“重建”首轮时使用）
            rehydrate_msgs: List[dict] = []

            # 透传模型配置 + 归一化 expire_at
            cfg = self._model_config()
            self._normalize_expire_at(cfg)

            payload: Dict[str, Any] = {
                "model": model_name,
                "input": [],
                "stream": False,
            }
            previous_id = state.last_id if not first_round else None
            if previous_id:
                payload["previous_response_id"] = previous_id

            for k, v in cfg.items():
                if k == "model":
                    continue
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

            # 调用（带重试）
            last_err: Optional[Exception] = None
            for attempt in range(self._max_retries):
                if attempt > 0:
                    await asyncio.sleep(self._retry_sleep * attempt)
                try:
                    status, data = await self._post_responses(payload)

                    # previous_response_id 失效：新建 +（可选）重放上下文
                    if self._is_prev_id_invalid(status, data):
                        logger.warning("[ArkResponses] previous_response_id invalid -> recreate session and retry")
                        state = _RespState(last_id="", tools_sent=False)
                        self._state[skey] = state
                        await self._save_state()

                        if self._rehydrate_on_recreate and contexts:
                            rehydrate_msgs = self._contexts_to_ark_messages(contexts)
                            logger.info("[ArkResponses] rehydrate on recreate: %d messages injected", len(rehydrate_msgs))

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

                        status, data = await self._post_responses(payload)

                    if not (isinstance(data, dict) and status < 400):
                        raise RuntimeError(f"ArkResponses error http={status}, data={data}")

                    usage = self._parse_usage(data)
                    assistant_text, fcs = self._parse_outputs(data)
                    new_id = str(data.get("id") or "")

                    if new_id:
                        state.last_id = new_id
                    if (first_round or rehydrate_msgs) and ("tools" in payload):
                        state.tools_sent = True
                    self._state[skey] = state
                    await self._save_state()

                    return self._build_llm_response(
                        assistant_text=assistant_text,
                        function_calls=fcs,
                        usage_info=usage,
                        response_id=new_id,
                        previous_response_id=previous_id,
                    )

                except Exception as e:
                    last_err = e
                    s = str(e)
                    if "429" in s or "Too Many Requests" in s:
                        if len(self._keys) > 1:
                            self._rotate_key()
                            continue
                    if "http=5" in s:
                        continue
                    if attempt == self._max_retries - 1:
                        logger.error("[ArkResponses] request failed after retries: %s", e)
                        raise
            if last_err:
                raise last_err
            raise RuntimeError("ArkResponses: unknown error")

    # -------------- 简化的“流式”包装：一次性产出 --------------
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
        resp = await self.text_chat(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            **kwargs,
        )
        resp.is_chunk = False
        yield resp
