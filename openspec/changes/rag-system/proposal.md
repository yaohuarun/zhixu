# Proposal

## Why

需要将本地文件与服务器目录中的资料统一转化为可持续更新、可追溯来源的问答知识库。仓库尚无应用代码，本变更建立完整的 RAG 前后端及 Docker 部署方案。

## What Changes

- 使用 Python/FastAPI 和 Vue3 创建知识库管理、数据源管理、任务监控与多轮问答界面。
- 接入文件、文件夹上传及服务器指定目录，支持 PDF（含扫描件）、DOC、DOCX、TXT。
- 本地 OCR 与 Word 转换；结构优先切块，超长内容递归切分，保留来源定位。
- 内容哈希增量更新；目录完整扫描成功后同步删除，失败时保留索引。
- 混合检索融合关键词和向量召回，使用 qwen3.7-text-rerank 重排序。
- 外部 DeepSeek 生成回答、qwen3.7-text-embedding-flash 向量化，提供流式回答与来源引用。
- 使用 PostgreSQL 保存业务数据、Qdrant 保存向量；Docker Compose 编排并持久化数据。
- 首版不包含登录与权限控制。

## Capabilities

### New Capabilities

- `knowledge-ingestion`: 知识库、上传与服务器目录数据源、增量同步及任务状态。
- `document-processing`: 格式解析、本地 OCR、结构切块及来源元数据。
- `hybrid-retrieval`: 关键词与向量召回、融合、外部重排序及索引版本管理。
- `conversational-rag`: 多轮会话、问题改写、流式回答与引用。
- `rag-deployment`: Docker 部署、模型配置、持久化及运行就绪检查。

### Modified Capabilities

无现有规格。

## Impact

新增前端、后端、后台 Worker、数据库迁移与容器部署文件。引入本地 OCR、DOC 转换、文档解析依赖，以及 DeepSeek 和阿里云百炼外部 API；文档文本会发送至配置的模型服务。上传、转换与 OCR 需要磁盘及 CPU 资源。无既有接口兼容性负担。
