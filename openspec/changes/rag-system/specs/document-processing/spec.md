# Spec Delta

## Purpose

将多种格式资料转换为可检索且保留原始来源的文本块，覆盖文字型与扫描型 PDF、Word 和纯文本。通过结构优先切分、合理长度限制及稳定处理版本，使检索片段具有足够上下文并支持后续增量更新。

## ADDED Requirements

### Requirement: Format parsing and local OCR
系统 SHALL 解析 PDF、DOC、DOCX、TXT；PDF SHALL 逐页判断文字可用性，对需识别页面在服务器本地执行 OCR，并支持文字与扫描页混合的文档。系统 MUST 按可恢复阅读顺序输出内容，记录原始页码；损坏、加密或无法解析输入 MUST 返回明确文件级错误。

#### Scenario: Mixed PDF
- **WHEN** 导入同时包含文字页和扫描页的 PDF
- **THEN** 系统直接提取文字页内容，对扫描页执行本地 OCR，合并结果并保留每个片段页码

#### Scenario: Legacy Word and plain text
- **WHEN** 导入 DOC、DOCX 或 TXT
- **THEN** 系统提取文字及可识别结构，并以章节、段落或原文位置定位，不伪造原始页码

#### Scenario: Unprocessable input
- **WHEN** 文档损坏、加密或文本解码失败
- **THEN** 系统记录明确失败原因且不以不完整新版本替换原有生效版本

### Requirement: Structure-first chunking
系统 SHALL 优先按章节组织段落，尽量保持列表引导句和列表项完整、表格独立；PDF 页边界 MUST 不成为强制切块边界。结构不可可靠识别时 SHALL 回退为段落、句子、长度逐级递归切分。

#### Scenario: A section spans pages
- **WHEN** 一个 PDF 章节跨页且总长度未达到切分上限
- **THEN** 系统允许同一块跨页并记录准确页码范围

#### Scenario: Oversized table
- **WHEN** 表格超出块长度上限
- **THEN** 系统按行拆分并重复表头；单行仍超限时继续拆分并保留表头及来源信息

#### Scenario: Uncertain OCR structure
- **WHEN** OCR 输出无法可靠判断标题层级
- **THEN** 系统合并可识别断行后按段落和句子递归切分，不将猜测标题作为确定层级

### Requirement: Configurable chunk budgets
系统 SHALL 提供默认目标 600 tokens、最大 900 tokens、超长内容拆分重叠约 80 tokens 的可配置切块参数；计数 MUST 包含检索文本附加标题。完整章节边界不加入重叠，短段落仅在同一章节内合并；token 计数方式与处理配置 MUST 被记录。

#### Scenario: Split oversized content
- **WHEN** 一个章节或段落超过最大长度
- **THEN** 系统尽量在句子边界拆分并提供受预算约束的重叠，每块计入标题后不超过所配置计数器上限

#### Scenario: Excessively long title
- **WHEN** 标题路径占用过多预算
- **THEN** 系统缩短检索前缀以留出正文空间，保留完整标题元数据且仍遵守长度上限

### Requirement: Original content and provenance
系统 SHALL 分别保存用于展示引用的原文和包含文档名称、章节路径的检索文本；每块 SHALL 关联文档版本、序号、内容哈希、处理配置版本和来源位置。相同检索文本与相同 Embedding 配置 SHALL 可复用已有向量，但 MUST 更新来源元数据。

#### Scenario: Reuse unchanged content
- **WHEN** 文档修改后某块检索文本和 Embedding 配置仍与已有缓存一致
- **THEN** 系统复用其向量且为新文档版本保存正确页码、章节与原文位置

#### Scenario: Processing configuration changes
- **WHEN** 切块规则、token 计数器、解析或 Embedding 配置改变
- **THEN** 系统识别需要重新处理的文档，旧版本继续生效直到新版本成功完成
