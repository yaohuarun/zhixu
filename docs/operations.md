# 运维操作

## 部署与检查

按根 README 的初始化步骤启动。模型接口是三类独立配置；Embedding 与 Rerank 使用 Model Studio 原生接口，不能将聊天 completions URL 用作这两类接口。区域、工作空间、模型授权必须匹配。DeepSeek 模型 ID 按实际账号配置。

目录文件挂载到 `/data/sources`；数据库保存的是容器路径。源目录只读，解析使用 `/data/storage` 内不可变快照。Worker 限制为 8 GB 内存、2 CPU；PDF 最大页数、渲染像素和解析时间另有环境变量限制。大量文档先使用小批次测量资源，再调整并发。

首次 `python -m app.parsing --init-ocr` 会下载本地模型并完成一次推理，成功后写入资产卷 `.ready`。正常解析读取本地资产；资产不齐或损坏时应重新初始化。OCR不使用外部文字识别API，生成与检索仍会向已配置模型服务发送文本。

前端 Nginx 使用 Docker DNS 动态解析 API 服务地址，避免 API 单独重建后继续请求旧地址。代理关闭响应缓冲以传递 SSE；相关行为见 [Nginx proxy_pass 文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)。

## 协调备份

在项目根执行 `python scripts/backup.py --output backups/<新的目录名>`。脚本停止当前运行的 API、Worker、前端，导出数据库、Qdrant 各集合快照及 storage/models 卷，最后恢复原运行服务。确认没有其他客户端直接写入这些存储。备份目录不能预先存在，避免覆盖旧备份。

保留 `.env`、Compose 配置、镜像版本以及服务器源目录的独立备份。备份包中的 manifest 记录向量快照对应集合。`.env` 含密钥，应独立安全保存。

## 恢复到空环境

不要在运行中的业务环境直接执行以下恢复；先创建空 Compose 项目与卷，并保持 API/Worker 停止。

1. 使用备份对应代码、镜像、配置，启动 PostgreSQL 和 Qdrant。
2. 将 `postgres.dump` 的二进制内容通过标准输入传给 `docker compose exec -T postgres pg_restore -U rag -d rag --clean --if-exists`。Windows 请使用 Python 的文件流作为 subprocess stdin，避免 PowerShell 文本管道改写字节。
3. 将 `files-models.tar.gz` 文件流传给 `docker compose run --rm --no-deps -T --entrypoint tar api -C /data -xzf -`。两个卷必须为空，避免混入另一个备份的快照。
4. 逐个向 Qdrant `POST /collections/<集合名>/snapshots/upload?priority=snapshot` 上传 manifest 中的快照。可使用 `curl.exe -X POST -F snapshot=@<文件> <Qdrant地址>/collections/<集合名>/snapshots/upload?priority=snapshot`。只使用备份对应集合名，不覆盖另一个业务的集合。
5. 校验数据库生效版本、对应 chunks、Qdrant point 数量和原文快照存在；健康检查通过后启动 API，抽查来源预览与检索，然后启用 Worker 和前端。

恢复过程失败时保留写入停止状态；重新创建空卷并从同一协调备份恢复，不拼接不同时间点的数据库和向量快照。

## 回滚与故障处理

应用回滚使用上一版镜像与兼容配置；存在不兼容数据库迁移时，从协调备份整体恢复。Embedding 模型、维度或接口改变后，通过“重建向量索引”生成新集合，覆盖校验通过后切换；不要原地修改旧集合维度。

任务失败可查看文件级原因并重试。Worker 崩溃后按租约自动回收，旧 owner 不能发布。目录不可访问或任一子目录枚举失败时不执行删除。后台清理失败仍以 PostgreSQL 当前版本过滤检索，不把旧向量当作新文档使用。

生产部署当前不含登录与权限控制，按约定通过本机地址和部署网络提供访问。
