# -*- coding: utf-8 -*-
from astrbot.api.star import Star, Context
from astrbot.api import logger

# 绝对导入“类”并显式引用：
# 1) 触发 ark_responses_provider 模块中的 @register_provider_adapter 注册（框架约定）
# 2) 避免静态审查工具将导入判定为未使用
from astrbot_plugin_provider_ark_responses.ark_responses_provider import (
    ArkResponsesProvider as _ArkProviderRef,
)


class ArkResponsesProviderLoader(Star):
    """插件入口（仅一次）。"""

    def __init__(self, context: Context):
        super().__init__(context)
        _ = _ArkProviderRef  # 确保 Provider 被加载和注册（见上方说明）
        logger.info(
            "[ArkResponsesProviderLoader] Ark Responses Provider 模块已加载"
        )

    async def terminate(self):
        # 如需在停用时清理资源，可在此处调用 provider.close()
        pass
