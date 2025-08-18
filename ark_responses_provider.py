# -*- coding: utf-8 -*-
from astrbot.api.star import Star, Context
from astrbot.api import logger

# Import for side-effects: this triggers the @register_provider_adapter
# inside the provider module (framework convention).
# 通过导入 provider 模块以触发 register_provider_adapter 完成注册（框架约定）
from astrbot_plugin_provider_ark_responses import ark_responses_provider  # noqa: F401


class ArkResponsesProviderLoader(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        logger.info(
            "[ArkResponsesProviderLoader] Ark Responses Provider 模块已加载"
        )

    async def terminate(self):
        # 如需在停用时清理资源，可在此处调用 provider.close()
        pass
