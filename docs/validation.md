# 验收记录

执行环境：Windows、Python 3.12、Docker Desktop Linux 容器。测试使用独立 PostgreSQL `rag_test` 与真实 Qdrant；浏览器模型接口是本地 HTTP 契约夹具，不能代表 DeepSeek/Qwen 实际服务和检索质量。

## 已验证

- 后端 28 项测试通过，覆盖输入校验、格式解析、结构切块、接口契约，以及真实 PostgreSQL/Qdrant 的增量更新、版本发布、删除保护、重建、队列恢复和引用。包含 Embedding 两种索引字段、改写关闭思考和截断错误、回答思考开关及输出预算；缺配置测试显式隔离本地真实密钥。
- MyPy 17 个源码文件通过，Ruff 通过；Vue 类型检查及 Vite 生产构建通过。
- Playwright 三条浏览器流程通过：文件导入到多轮问答、失败任务重试、文件夹接入与管理；覆盖来源片段、刷新恢复、取消和重试不重复用户消息。
- `docker compose config --quiet` 通过；API、前端、迁移、CPU OCR/LibreOffice Worker 镜像全部构建，迁移与首次启动成功。Worker 锁定 Paddle 3.0.0、PaddleOCR/PaddleX 3.0.3 及完整 OCR 依赖约束。
- 禁用网络的 Worker 容器识别中文/英文扫描 PDF、混合 PDF，保留混合页的 `text/ocr` 提取方式与页码 1/2；文字 PDF 强制 OCR 成功。本地模型初始化与 CPU 运行检查通过。
- 两个并发 LibreOffice DOC→DOCX 转换均保留标题、列表、表格与章节。样例 DOC 经 DOCX→ODT→DOC 生成，避免当前 LibreOffice 直接导出旧 DOC 时压平表格的现象；生产处理仍为 DOC→DOCX。
- 六种样例导入真实 PostgreSQL/Qdrant：TXT、DOCX、DOC、文字 PDF、扫描 PDF、混合 PDF；未变化文件跳过、两轮带来源问答通过。耗时见下表，模型为 HTTP 契约夹具。
- 全部 Compose 容器强制重建后，6 份文档、8 块、6 个成功索引任务、4 条会话消息保留且可读；源目录写入返回只读文件系统，OCR 资产 `.ready` 保留。
- 三条 Playwright 流程也在 Docker 前端/API/Worker 下通过。Nginx SSE 验收片段于 0.676、0.783、0.890 秒到达，未合并缓冲。
- 修复 API 单独重建后 Nginx 缓存旧容器地址导致的 502：使用 Docker DNS 动态解析上游；保持前端容器运行、仅重建 API 后代理健康检查返回 200，修复后三条容器浏览器流程再次通过。
- `backups/acceptance-20261008` 协调备份成功，恢复到独立空项目 `rag-restore-check` 后，确认 6 个生效原文快照文件存在，8 个当前文本块对应的向量逐个可读；数据库保留 28 个知识库审计记录（含已删除验收库）。恢复报告状态为 `passed`，成功后停止测试存储服务。

| 样例 | 导入并索引耗时（秒） | 解析元素 |
| --- | ---: | ---: |
| TXT | 1.837 | 1 |
| DOCX | 0.776 | 6 |
| DOC | 2.596 | 6 |
| 文字 PDF | 0.967 | 1 |
| 扫描 PDF | 83.373 | 1 |
| 混合 PDF | 79.303 | 2 |

CPU OCR 的耗时包含隔离子进程加载结构识别模型；小样本结果不代表批量吞吐或复杂扫描质量。

## 真实服务验收

- 三个真实模型调用通过：Embedding 1024 维、0.707 秒；Rerank 0.699 秒，相关/无关得分 0.817447/0.234205；DeepSeek 实际配置模型为 `deepseek-flash`，非流式检查 1.434 秒。
- 使用实际服务和本地 OCR 的六格式端到端验收通过，扫描 PDF 124.256 秒、混合 PDF 110.220 秒；两轮来源问答分别 3.082/3.445 秒，各有 8 个来源，追问改写为“知识库是否支持扫描件？”，无能力降级。延迟包括隔离解析、OCR 模型加载及索引流程，不代表纯服务延迟。见 [模型验收记录](reports/model-acceptance.json)。
- 三文档中文检索集：关键词首位命中 5/6；向量、融合、重排首位命中均为 6/6。校准阈值 0.5481 在独立四问复核中通过 4/4，仅用于该小语料，未设置为全局默认值。见 [检索评估](retrieval-evaluation.md)。
- 后续真实长历史复现中，默认思考模式耗尽改写的 512 token、返回空最终内容；关闭思考后正常返回。修复为改写固定关闭思考、回答默认关闭思考且输出预算 4096；历史作为数据传入专用改写器，并限制单行100字。实际追问复测返回“对项目经理有什么要求？”。原截断回答使用同一请求标识重试后完成，没有新增重复用户消息。

## 可重复命令

按 README 准备独立测试库并完成迁移，然后在项目根执行：

```powershell
$env:RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55432/rag_test'
.venv/Scripts/python -m pytest backend/tests -q
.venv/Scripts/python -m mypy --config-file backend/pyproject.toml backend/app
.venv/Scripts/python -m ruff check backend/app backend/tests scripts
npm --prefix frontend run build
# 释放 8001、9009、5174 后，启动独立测试 API/Worker/模型夹具并自动关闭：
.venv/Scripts/python scripts/browser_contract.py
.venv/Scripts/python scripts/generate_acceptance_fixtures.py
```

首次运行浏览器测试若缺 Chromium，可在 frontend 下执行 `npx playwright install chromium`。单独运行 `npx playwright test` 需要预先启动上述测试服务器，推荐使用统一脚本。

Docker/OCR 契约验收只在专用测试部署执行。以下 PowerShell 命令使用合成模型接口，不能用于生产模型配置；第二个终端保持 HTTP 夹具运行：

```powershell
.venv/Scripts/python -m uvicorn contract_model_server:app --app-dir backend/tests --host 0.0.0.0 --port 9009
```

在测试终端（Docker Desktop 提供 `host.docker.internal`）：

```powershell
.venv/Scripts/python scripts/generate_acceptance_fixtures.py
docker compose build
docker compose up -d postgres qdrant
docker compose run --rm migrate
docker compose run --rm --no-deps worker python -m app.parsing --init-ocr
docker run --rm --network none --memory 8g --cpus 2 -e RAG_OCR_MODEL_ROOT=/data/models -v rag-workspace_model_data:/data/models -v "${PWD}/data/acceptance:/fixtures:ro" -v "${PWD}/scripts:/checks:ro" rag-workspace-worker python /checks/offline_ocr_check.py
# 手工驱动索引验收时停下常驻 Worker，避免竞争测试任务：
docker compose stop worker
docker compose -f compose.yaml -f scripts/compose.contract.yaml run --rm --no-deps -v "${PWD}/data/acceptance:/fixtures:ro" -v "${PWD}/scripts:/checks:ro" worker python /checks/docker_acceptance.py --keep
# 记录输出的知识库及会话 ID，再启动完整契约部署：
docker compose -f compose.yaml -f scripts/compose.contract.yaml up -d --wait
$env:RAG_UI_URL='http://127.0.0.1:8080'
cd frontend
npx playwright test
cd ..
docker compose -f compose.yaml -f scripts/compose.contract.yaml up -d --force-recreate --wait
.venv/Scripts/python scripts/persistence_check.py <知识库ID> <会话ID>
.venv/Scripts/python scripts/proxy_stream_check.py <知识库ID>
docker compose run --rm --no-deps -v "${PWD}/scripts:/checks:ro" worker python /checks/mount_check.py
.venv/Scripts/python scripts/backup.py --output backups/<新的目录>
.venv/Scripts/python scripts/restore_check.py backups/<新的目录>
```

恢复检查要求 `rag-restore-check` 不存在，端口 55433/56334 空闲。它使用独立卷，读取生效版本快照文件并逐一确认对应 Qdrant 点。检查成功后停止该测试项目的存储服务；下一次复验前仅清理这个专用测试项目。

验收后删除所创建的知识库和会话，运行 `docker compose up -d --wait` 恢复正常模型配置，停止合成模型服务器。Linux Docker 需为契约容器设置 `host.docker.internal:host-gateway`，或者改为可达的测试服务器地址。

真实模型与检索效果验证（需要有效配置）：

```powershell
.venv/Scripts/python scripts/live_smoke.py
# 导入 ingestion.txt、synchronization.txt、retrieval.txt 后，使用对应知识库 ID：
.venv/Scripts/python scripts/evaluate_retrieval.py <知识库ID>
```

## 规格证据对应

| 规格要求 | 证据 / 状态 |
| --- | --- |
| 知识库和数据源管理 | integration、workflow 管理与错误流程 |
| 文件和文件夹上传 | core 路径/大小；integration 增量；workflow 文件夹与拒绝 |
| 服务器目录同步 | integration 完整/失败扫描；resilience 周期同步去重 |
| 增量和安全删除 | integration 旧版本保留与失败枚举不删除 |
| 后台任务与恢复 | queue 并发领取、租约；resilience 发布前和清理故障；workflow 重试 |
| 格式解析及本地 OCR | core TXT/DOCX/PDF；并发 DOC；扫描/混合/强制 OCR；禁网识别 |
| 结构优先切块 | core 章节、列表、长文、表格；resilience PDF 双栏、页边清洗 |
| 可配置切块预算 | core 计数器、超长重叠；resilience 模型长度拒绝重新切块 |
| 原文与来源 | core 解析位置；integration 增量缓存；chat 引用快照 |
| 混合候选召回 | integration 中文召回；core RRF 去重 |
| 外部重排序 | core 接口响应校验；resilience 降级；真实模型与中文评估通过 |
| 一致版本发布 | integration、resilience 新版失败/发布前故障/删除/清理重试 |
| Embedding 配置兼容 | integration 1024→512 独立集合切换；真实 1024 维和两种索引字段通过 |
| 持久多轮会话 | chat 并发、幂等、历史；workflow 刷新恢复 |
| 历史感知检索 | chat、workflow；真实 DeepSeek 成功将追问改写为独立扫描件问题 |
| 流式回答和引用 | chat SSE 事件与伪造编号；workflow 来源面板 |
| 中断和失败可见 | chat 上游失败；workflow 取消/重试 |
| Docker 部署 | Compose 校验、全部镜像与组件启动；容器浏览器完整流程 |
| 持久化和挂载 | 全部容器重建后文档/块/任务/会话保留；只读挂载；断网 OCR；协调恢复检查 |
| 模型独立配置 | core 缺密钥/脱敏；三类接口真实调用通过 |
| 就绪与资源限制 | core 依赖不可用返回503；resilience 加密/页数/解析超时清理；Compose CPU/内存限制 |
