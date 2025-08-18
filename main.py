# -*- coding: utf-8 -*-
from astrbot.api.star import Star, Context
from astrbot.api import logger

# Import for side-effects: this triggers the @register_provider_adapter inside the provider module.
# 导入 provider 模块以触发 register_provider_adapter 装饰器完成注册（框架约定）
from . import ark_responses_provider  # noqa: F401


class ArkResponsesProviderLoader(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        logger.info("[ArkResponsesProviderLoader] 已加载 Ark Responses Provider 模块")

    async def terminate(self):
        # 如需在停用时清理资源，可在此处调用 provider.close()
        pass
