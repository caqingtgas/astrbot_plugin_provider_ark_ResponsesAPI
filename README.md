# Volcengine Ark Responses API Provider for AstrBot

**在 AstrBot 使用火山方舟 (Volcengine Ark) 上下文缓存 API（Responses API）**

## 开始之前
- 如果你需要的是 **Context API**，请去 [这里](https://github.com/caqingtgas/astrbot_plugin_provider_ark_ContextAPI) 
- 目前 AstrBot 没有火山方舟的内置模板，直接用 OpenAI 兼容接口部署 **Responses API** 会**无法传入** `previous_response_id`，从而无法多轮与缓存命中。使用本插件可原生集成 Responses API。  
- **此能力为邀测**，需要在火山方舟提交工单申请开通；使用前还需完成：获取 API Key、开通模型服务、拿到 Model ID / Endpoint ID、开通缓存功能等前提条件。  


## 简介
为什么不用 Context API？它针对老模型，但**不支持 Function Calling**，限制了联网/工具能力。  
**Responses API** 是针对 **doubao-seed-1.6 系列**的缓存方案，采用**会话接力**：首轮创建一个 `response_id`，后续用 `previous_response_id` 接力输出 **新** `response_id`。更重要的是，它**支持 Function Calling**，可同时拥有“缓存降费 + 工具调用”。

### 功能
- **session上下文缓存**：自动串联 `previous_response_id`，输出 `resp_id` 与 `prev_id`。  
- **Function Calling**：自动按照 Responses API 的 `tools` 规范传参与回填。  
- **命中统计**：控制台打印 `cached_tokens` 等用量信息。  
- **`expire_at`更改传入**：官方文档这个参数需要传入UTC Unix时间戳，现在直接传入秒数，日志会打印到期 UTC 时间。
- **重置即删**：`/reset` 清空上下文时，默认调用 `DELETE /responses/{id}` 回收远端缓存。  
> 注意：工具（`tools`） **只能在首轮声明**；本插件已自动控制。


## 使用
在配置文件中加入：
```json
{
  "id": "任意",
  "type": "ark_responses",
  "provider_type": "chat_completion",
  "enable": true,
  "key": ["你的APIkey"],
  "api_base": "https://ark.cn-beijing.volces.com/api/v3",
  "timeout": 60,

  "auto_delete_on_reset": true,
  "max_retries": 3,
  "retry_sleep": 0.2,
  "model_config": {
    "model": "doubao-seed-1-6-250615",
    "store": true,
    "caching": { "type": "enabled" },
    "temperature": 1,
    "expire_at": 范围0到259200，即小于3天,
    "thinking": { "type": "disabled或enabled或auto" }
  }
}
```


## 其他
- 如何稳定写入缓存？——需要确保每一轮都caching: {"type":"enabled"}；如果中途有一轮关闭，则后续轮次都无法写入缓存。
- 工具（Function Calling）何时声明？——只在首轮声明 tools。
- instructions？——会影响缓存，尽量别用，普通的 system 消息（AstrBot 注入）属于 input 内容，不会触发此限制。
- 日志时间？——是 UTC，日志里打印 utc=... 是世界时；若在北京时间查看，本地时间 = utc + 8 小时。