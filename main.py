# -*- coding: utf-8 -*-
from astrbot.api.star import Star, Context
from astrbot.api import logger

# 绝对导入“类”并显式引用，避免“未使用导入”提示；
# 同时确保模块被导入以触发 @register_provider_adapter 的注册（框架约定）
from astrbot_plugin_provider_ark_responses.ark_responses_provider import (
    ArkResponsesProvider as _ArkProviderRef,
)


class ArkResponsesProviderLoader(Star):
    """插件入口（仅一次）。"""

    def __init__(self, context: Context):
        super().__init__(context)
        # 显式引用，避免静态审查工具将导入判定为未使用
        _ = _ArkProviderRef
        logger.info(
            "[ArkResponsesProviderLoader] Ark Responses Provider 模块已加载"
        )

    async def terminate(self):
        # 如需在停用时清理资源，可在此处调用 provider.close()
        pass
