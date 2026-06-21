"""spineagent.llm —— 真实 LLM provider 适配器(挂在 corespine LLMProvider 缝后面)。

离线默认仍是 corespine 的 MockProvider;真实后端(Anthropic / OpenAI 兼容)各走可选 extra
延迟 import,各用各自的【官方 SDK 原生形状】,绝不把一家套进另一家的 API(不做 shim)。
"""
