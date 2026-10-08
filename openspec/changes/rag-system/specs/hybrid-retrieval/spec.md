# Spec Delta

## Purpose

在指定知识库中同时利用关键词匹配与向量语义匹配召回相关文本，再融合并重排序候选片段。通过生效版本校验与可观察失败策略，确保问答只使用当前资料，且索引更新过程不造成暂时的检索空缺。

## ADDED Requirements

### Requirement: Hybrid candidate retrieval
系统 SHALL 在用户选定知识库内同时执行支持中文的关键词召回及 qwen3.7-text-embedding-flash 查询向量召回，融合排名并按块身份去重；召回数及融合参数 SHALL 可配置。

#### Scenario: Chinese hybrid query
- **WHEN** 用户提交包含中文关键词和自然语言描述的问题
- **THEN** 系统分别取得关键词与语义候选，融合后仅返回所选知识库当前生效版本片段

#### Scenario: Identical candidate from two branches
- **WHEN** 同一块同时被两个召回分支命中
- **THEN** 系统融合其排名且候选集中只保留一个该块

### Requirement: External reranking
系统 SHALL 使用外部 qwen3.7-text-rerank 对融合后的候选重排序，并选取可配置数量及上下文预算内的片段。输入 MUST 遵守配置服务的文档数和 token 限制；超时或失败 SHALL 退回融合排序并向界面及日志标记降级。

#### Scenario: Successful reranking
- **WHEN** 融合候选存在且重排序调用成功
- **THEN** 系统按重排序结果选择上下文，同时保留每个候选的来源定位

#### Scenario: Rerank outage
- **WHEN** 重排序接口超时、限流或返回不可用结果
- **THEN** 系统在有限重试后使用融合排序继续回答，并标记重排序降级

### Requirement: Version-consistent index publication
系统 SHALL 仅检索当前生效文档版本；更新 MUST 先完成新版本文本及向量索引，再切换生效版本，之后清理旧索引。新版本失败 MUST 保留旧版本检索能力；已删除或正在删除的知识库和文档 MUST 不再出现在新检索结果中。

#### Scenario: Query during update
- **WHEN** 文档新版本正在解析或索引
- **THEN** 检索继续使用其旧生效版本，不混入未完成新版本

#### Scenario: Publication succeeds but cleanup fails
- **WHEN** 新版本生效后旧向量清理失败
- **THEN** 新检索只返回新版本内容，系统记录并重试清理旧数据

### Requirement: Embedding configuration compatibility
系统 SHALL 为知识库索引记录 Embedding 模型、服务、向量维度和配置版本；默认维度为 1024。模型或维度变更 MUST 创建可重建的新索引配置，不允许查询向量与索引配置不一致。

#### Scenario: Change embedding configuration
- **WHEN** 管理员改变 Embedding 模型或维度
- **THEN** 系统使用独立新索引重建，重建完成前使用旧配置检索，成功后切换而不混用不同空间的向量
