# Spec Delta

## Purpose

为用户提供基于知识库的多轮问答体验，利用已有会话理解后续问题，流式显示回答并提供可核查的文档引用。通过上下文预算、无依据回答策略和模型错误反馈，让会话在资料不足或外部服务失败时仍有明确状态。

## ADDED Requirements

### Requirement: Persistent multi-turn conversations
系统 SHALL 支持创建、查看、重命名和删除会话，保存用户及助手消息与其状态，并为每次问答记录选定知识库。历史 MUST 按可配置预算裁剪；同一会话并发提问 SHALL 被串行处理或明确拒绝。

#### Scenario: Resume a conversation
- **WHEN** 用户重新打开已有会话
- **THEN** 系统展示持久化消息、回答状态及已有来源引用

#### Scenario: Concurrent submissions
- **WHEN** 同一会话有一个回答进行中又提交下一问题
- **THEN** 系统明确拒绝或排队，避免历史顺序不确定

### Requirement: History-aware retrieval
系统 SHALL 使用 DeepSeek 将依赖会话历史的问题改写为可独立检索的问题，保留原始问题用于展示；改写失败 SHALL 使用原始问题并标记降级。问答上下文 MUST 基于本轮检索，不直接将旧回答当作资料依据。

#### Scenario: Follow-up question
- **WHEN** 用户在上一轮讨论某产品后询问“它支持哪些格式”
- **THEN** 系统结合有限历史补全指代后检索，展示原始提问并使用本轮来源回答

### Requirement: Streaming grounded answers and citations
系统 SHALL 使用外部 DeepSeek 生成流式回答，附有可打开查看的来源片段。引用 MUST 对应本轮实际提供的片段及其文档版本；知识库片段 SHALL 被当作参考数据，不得覆盖系统指令。无可用依据时 MUST 明确说明资料不足，不编造引用。

#### Scenario: Answer with PDF evidence
- **WHEN** 检索得到相关 PDF 片段并完成回答
- **THEN** 前端逐步展示回答及引用，用户可查看文档名称、准确页码范围和支持片段

#### Scenario: Answer with Word evidence
- **WHEN** 引用来自 Word 或 TXT
- **THEN** 系统提供章节、段落或原文位置和片段，而不展示伪造 PDF 页码

#### Scenario: No relevant evidence
- **WHEN** 无候选或候选未达到配置的相关性判定条件
- **THEN** 系统明确说明知识库中没有足够依据，不生成虚构来源

### Requirement: Interrupted and failed answer visibility
系统 SHALL 为流式请求提供结束或错误事件，持久化已输出内容与最终状态。外部生成失败、客户端取消或连接断开 MUST 不被记录为完整成功回答；重试 MUST 避免重复用户消息及混合多个回答流。

#### Scenario: Generation interrupted
- **WHEN** 输出部分回答后模型调用失败或用户取消
- **THEN** 界面和历史标记回答未完成，用户可以发起独立重试
