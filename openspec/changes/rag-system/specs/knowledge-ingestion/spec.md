# Spec Delta

## Purpose

让用户在统一界面中创建知识库、导入本地资料或接入服务器目录，并持续查看同步和索引状态。通过增量处理与完整扫描保护机制，让知识库内容及时更新且不会因暂时的扫描故障被误删。

## ADDED Requirements

### Requirement: Knowledge base and data source management
系统 SHALL 提供创建、查看、修改和删除知识库的界面与接口，并允许每个知识库添加上传或服务器目录数据源。删除知识库 MUST 清理其文件、索引与关联业务数据并展示处理结果。

#### Scenario: Create a knowledge base
- **WHEN** 用户创建知识库并添加数据源
- **THEN** 界面显示知识库、数据源配置及其文档和任务入口

#### Scenario: Delete a knowledge base
- **WHEN** 用户确认删除知识库
- **THEN** 系统停止该知识库新任务与检索，异步清理其数据并显示成功或可重试的失败状态

### Requirement: File and folder upload
系统 SHALL 支持单文件、多文件和文件夹上传，保存相对路径；允许 PDF、DOC、DOCX、TXT。系统 MUST 拒绝越界路径、不支持格式及超过可配置大小限制的输入，并返回明确原因。

#### Scenario: Upload a folder
- **WHEN** 用户上传包含子目录的文件夹
- **THEN** 系统保留合法文件的相对路径，为每个文件展示导入状态，并明确列出被拒绝文件的原因

#### Scenario: Replace an uploaded file
- **WHEN** 用户向同一上传数据源的同一相对路径再次上传文件
- **THEN** 系统按内容变化更新该文档而不产生重复文档；上传批次中缺失的旧文件不被自动删除

#### Scenario: Reject an unsafe path
- **WHEN** 上传路径包含能够逃逸存储根目录的路径段
- **THEN** 系统拒绝该文件且不写入存储根之外的位置

### Requirement: Server directory synchronization
系统 SHALL 对容器内配置的服务器目录递归执行手动同步和可配置周期同步；目录 MUST 位于允许的数据源挂载根内，扫描不得通过符号链接逃逸根目录。

#### Scenario: Synchronize a mounted directory
- **WHEN** 用户配置可访问的挂载目录并启动同步
- **THEN** 系统递归发现支持格式文件，显示新增、修改、未变化和删除数量

#### Scenario: Reject an out-of-root directory
- **WHEN** 数据源路径或符号链接解析后位于允许根之外
- **THEN** 系统拒绝配置或跳过越界条目，并显示原因

### Requirement: Incremental changes and safe deletion
系统 SHALL 用数据源和相对路径识别文档、用内容哈希判断变化；未变化且处理配置相同的文件 MUST 跳过重新解析和模型调用。仅在目录完整扫描成功后 SHALL 移除该数据源中已不存在文档；无法枚举全部目录、权限错误或目录不可访问 MUST 阻止该轮删除。

#### Scenario: Process additions and changes
- **WHEN** 成功扫描发现新增文件、内容变化文件和未变化文件
- **THEN** 系统只为新增及变化文件创建处理任务，保留未变化文件当前索引

#### Scenario: Confirm a deleted file
- **WHEN** 完整成功扫描确认先前文档已不存在
- **THEN** 文档不再参与检索，系统清理其索引并记录删除结果

#### Scenario: Preserve documents during failed scanning
- **WHEN** 根目录或任一需扫描子目录不可访问，或枚举未完整结束
- **THEN** 系统记录失败原因且不因该轮扫描缺失结果删除任何已有文档

### Requirement: Observable and retryable background jobs
系统 SHALL 异步处理导入、同步、解析和索引任务，显示排队、运行、成功、失败状态以及进度、文件级错误，并支持失败任务重试。重复同步或重试 MUST 不创建重复的生效文档版本。

#### Scenario: Retry after worker interruption
- **WHEN** 后台任务被中断后恢复或用户重试失败任务
- **THEN** 系统恢复或重新处理待完成项，最终只有一个对应输入版本生效
