# Spec Delta

## Purpose

提供能够在 Docker 环境中启动完整 RAG 前后端和后台处理流程的部署方式，并保证文件、业务记录与向量数据持久保存。通过外部模型配置、目录挂载说明及依赖就绪检查，使部署者能够稳定运行、诊断故障和恢复服务。

## ADDED Requirements

### Requirement: Docker Compose deployment
系统 SHALL 提供 Docker Compose 部署前端、FastAPI、独立 Worker、PostgreSQL 和 Qdrant，包含本地 OCR 与 DOC 转换依赖；提供启动、迁移、目录挂载、停止和备份恢复说明。首版 SHALL 无需登录使用，且不提供权限管理功能。

#### Scenario: Fresh deployment
- **WHEN** 部署者按文档配置模型密钥、持久化卷及数据源挂载并启动
- **THEN** 所有组件进入可诊断运行状态，用户可打开前端创建知识库并执行导入和问答

### Requirement: Persistent storage and mounts
系统 SHALL 持久化上传文件、业务数据库、向量数据库及所需模型资产；服务器数据源 SHALL 以只读目录挂载接入。服务重建 MUST 不丢失已持久化知识库、任务和会话数据。

#### Scenario: Recreate containers
- **WHEN** 部署者保留持久化卷并重建容器
- **THEN** 知识库、索引和会话仍可使用，未完成任务可以恢复

### Requirement: Separate external model configuration
系统 SHALL 在后端分别配置生成、Embedding、Rerank 的服务地址、密钥、模型 ID、超时和有限重试策略；默认模型对应 DeepSeek、qwen3.7-text-embedding-flash 与 qwen3.7-text-rerank。密钥 MUST 不出现在前端响应、构建资源和日志中。

#### Scenario: Missing model configuration
- **WHEN** 所需模型地址、密钥或模型 ID 缺失
- **THEN** 系统显示对应能力未配置的错误，允许修复配置且不泄露密钥

### Requirement: Readiness and resource limits
系统 SHALL 提供存活与就绪状态，数据库或向量服务不可用时 MUST 不报告相关能力就绪；OCR、转换、上传及模型调用 SHALL 设置可配置资源或时间限制，失败可在任务或请求状态中诊断。

#### Scenario: Vector database unavailable
- **WHEN** Qdrant 无法连接
- **THEN** 就绪状态显示依赖异常，索引与检索返回可诊断错误且不将未完成任务标记成功
