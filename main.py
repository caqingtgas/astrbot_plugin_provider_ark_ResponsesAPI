# -*- coding: utf-8 -*-
from astrbot.api.star import Star, Context
from astrbot.api import logger

# 用相对导入，避免目录大小写在不同环境下不一致导致的 ModuleNotFoundError
from .ark_responses_provider import ArkResponsesProvider as _ArkProviderRef


class ArkResponsesProviderLoader(Star):
    """插件入口（仅一次）。"""

    def __init__(self, context: Context):
        super().__init__(context)
        # 触发 ark_responses_provider 中的 @register_provider_adapter 注册。
        # 注意：此引用仅用于“加载并注册”，并不直接在此文件中使用。
        _ = _ArkProviderRef  # 确保 Provider 被加载和注册
        logger.info(
            "[ArkResponsesProviderLoader] Ark Responses Provider 模块已加载"
        )

    async def terminate(self):
        # 如需在停用时清理资源，可在此处调用 provider.close()
        pass
