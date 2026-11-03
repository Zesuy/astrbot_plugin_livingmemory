# LivingMemory 生产定制版 (astrbot_plugin_livingmemory)

面向 AstrBot 平台的长期记忆生命周期管理与原始聊天记录受控检索插件。基于社区原版深度定制，专为高频 QQ 群聊生产环境设计，在保留自动事实提炼与关系图谱的基础上，引入了**零主模型唤醒的被动捕获机制**与**面向原始消息的轻量采样检索工具集（overview / style_sample）**。

---

## 1. 项目名称与定位

- **插件标识**：`astrbot_plugin_livingmemory`
- **显示名称**：LivingMemory（生产定制轻量版）
- **定位**：解决 AstrBot 在真实群聊交互中“长期记忆空洞”、“日报周报抄袭竞品群报”、“跨午夜时间漂移”、“模仿群友说话断章取义”以及“监听群聊导致高频唤醒主模型产生额外 Token 消耗”等核心痛点的高性能记忆插件。

---

## 2. 上游来源与血统

- **官方上游**：[lxfight/astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（开源许可协议：AGPL-3.0），作者 lxfight。
- **当前元数据版本**：`2.7.0-beta.2`（补齐自生产容器镜像基线）。
- **生产定制基线**：
  - 稳定基线基于上游 Commit `8985ae6`；
  - 于 2026-10-23 增量合并上线了轻量检索模块 Commit `e69ee96f422e480f948093ec87bff64abee1f529`。

---

## 3. 为什么做深度定制

上游原版 LivingMemory 重点偏向于单人长周期伴随与通用事实沉淀，但在高活跃群聊场景中暴露了明显的工程与业务缺陷：

1. **群聊总结抄袭别家 Bot**：当群里有其他机器人定时发送“今日群报”或总结时，若单纯依靠关键词召回，主模型极易把别人发出的总结当作唯一事实甚至整篇抄袭，忽略了真实的群友聊天过程。
2. **跨午夜时间概念混乱**：深夜或凌晨提问（如 00:30 提问“今天聊了什么”）时，常规大模型常把“今天”理解为自然日的前一天；旧系统未对北京时间半闭半开区间进行严密锚定，造成检索范围漂移。
3. **首屏全看凌晨水群**：原始记录若直接按时间升序分页读取，第一页几乎全被凌晨 0点～6点 的零散闲聊占满，真正白天的活跃话题被全部挤出上下文窗口。
4. **群友风格模仿片面孤立**：模仿某个群友说话时，原版只能靠几句模糊记忆或孤立梗句，缺乏该用户在一定时间跨度内、排除表情包与命令词后的纯文本（Plain 组件）语料样本。
5. **被动归档的高昂唤醒代价**：若为了记录聊天而在 AstrBot 中给所有群消息挂钩子，常规插件会频繁唤醒主大模型执行 Filter 校验，造成巨大的 Token 浪费和交互卡顿。
6. **拒绝 `81d5fd4` 重型分面导航的权衡决策**：上游后续分支尝试引入重型分面索引（Faceted Indexing）和全局人物目录。经我们在生产前期的综合评估，重型索引不仅显著增加消息写入时的 SQLite 锁争用和延迟，而且在群聊实际场景中未带来召回质量的实质改善。因此，我们决定**坚决拒绝引入重型导航分支，保持轻量采样检索架构**。

---

## 4. 核心功能与指令/工具一览

### 4.1 管理指令

```text
/lmem status        # 查看当前长期记忆库容量、图谱节点数、会话统计与存储状态
/lmem summarize     # 手动触发当前会话未处理消息的长期记忆提炼与归档
/lmem search <词>   # 测试关键词与向量混合检索召回效果
```

### 4.2 大模型专属 Function Call 工具

插件向 AstrBot 注册了原生函数工具，其中 `query_chat_events` 是本次深度定制的核心模型入口：

#### 核心增量工具：`query_chat_events`
专门用于查询当前会话的原始历史聊天记录。工具支持 5 种精细操作模式：

| 模式 (`mode`) | 适用场景 | 核心机制 |
| :--- | :--- | :--- |
| **`overview`** | 时段群聊概览（日报/周报/回顾） | **未知话题先取样**。将时间窗分成 8 层，按消息序号分层均匀取样，返回密度分布；不按作者过滤。 |
| **`style_sample`** | 拟人/模仿具体群友说话方式 | **抽取本人真实纯文本发言**。自动剔除表情、图片、机器人命令；引用单独隔离；跨时间桶均匀取样。 |
| **`lookup`** | 查询已知明确话题 | 结合 `keywords` 和指定人物进行针对性命中，不做全群扫库。 |
| **`time_read`** | 围绕采样点补读上下文 | 在 `overview` 发现线索后，针对指定起止时间窗口补读完整的连续对话。 |
| **`read`** | 展开长消息原文 | 配合 `evidence_handle` 展开被截断的长文本消息。 |

- **关键参数说明**：
  - `query`：用户原始问题（如“今天我没发言时聊了什么”、“总结最近一周的话题”）。
  - `time_range`：支持“今天”、“昨天”、“昨晚”（固定为昨日18:00至今日06:00）、“最近7天”等自然表述。
  - `person`：支持具体 QQ/昵称；传入 `我` 自动绑定当前提问者，传入 `你` 自动绑定机器人自身。

#### 基础记忆工具：
- `recall_long_term_memory`：检索长期知识库与实体关系图谱。
- `memorize_long_term_memory`：当对话中出现关键偏好、约定或重要事实时，主动写入长期记忆库。

---

## 5. 关键算法与架构机制

### 5.1 被动群聊捕获机制（`PassiveGroupCaptureFilter`）

为了在群聊中完整保存上下文以供后续检索，同时**坚决不唤醒主模型**，定制了专用的自定义过滤器：

```python
# 核心架构（摘自 core/passive_group_capture.py 与 main.py）
@filter.custom_filter(PassiveGroupCaptureFilter, False)
async def handle_all_group_messages(self, event: AstrMessageEvent):
    """[Passive Filter Hook] Capture group messages without waking AstrBot."""
    return
```

- **始终返回 False**：`PassiveGroupCaptureFilter` 挂载在群消息前置过滤阶段。它在内部异步触发消息排队入库任务，但自身**始终向 AstrBot 返回 `False`**。
- **管线静默阻断**：由于返回了 `False`，AstrBot 认定该消息未命中唤醒条件，直接终止消息管线，不再调用任何 LLM 提示词生成与网络调用。
- **边界防御**：捕获时同步校验全局平台白名单（`enable_id_white_list`）与会话级插件开关，被禁用的会话不进行任何捕获。

### 5.2 时段分层采样算法（`overview`，`archive_sampling.py`）

为了解决“概览只看凌晨”和“密集刷屏掩盖正常讨论”的问题：

1. **8 等分时间层（Strata）**：将给定的时间范围（如今日、最近7天）在时间轴上均匀划分为 8 个桶（`bucket`）。
2. **消息序号空间分区（Ordinal Quantiles）**：
   - 先对每个非空层分配最多 5 条的种子候选片段；
   - 剩余正文预算按消息密度分配最多 3 条的补充片段；
   - 在每个层内，按照消息序号（Ordinal）而非单纯物理时间进行分段取样，彻底消除某一时间段几百条短消息爆发（Burst）导致的采样遮蔽。
3. **活动时间线探测**：当指定 `person=我` 时，计算该用户在整个时间窗内的发言边界，返回“无发言区间”，辅助大模型精准回答“我不在的时候发生了什么”。

### 5.3 人物语言风格采样算法（`style_sample`，`archive_sampling.py`）

为了让大模型真实学到群友的说话口吻：

1. **纯正文抽取与清洗**：仅保留由用户亲自提交的原生 `Plain` 文本，过滤所有图片、表情、文件，并滤除形如 `/`、`!`、`#` 开头的命令类输入。
2. **引用隔离**：将回复和引用内容剥离至 `quoted_context`，确保 `text` 字段 100% 属于本人用词。
3. **两端覆盖 + 二分取样**：在时间桶内先取首尾两端，再二分深入中间样本；同一 10 分钟最多选 2 条，去除完全重复文本，单次抽取上限为 32 条。

---

## 6. 配置全景指南

### 6.1 核心配置项

| 配置键 | 类型 | 说明 |
| :--- | :--- | :--- |
| `archive.query_enabled` | `bool` | 是否向大模型开放 `query_chat_events` 原始记录检索工具 |
| `embedding_provider` | `str` | 向量化模型提供方（为空则继承 AstrBot 全局默认） |
| `llm_provider` | `str` | 记忆总结与关系提炼的 LLM 提供方 |
| `rrf_k` | `int` | 混合检索倒数排序融合（RRF）常数（推荐 60） |
| `decay_rate` | `float` | 长期记忆随时间衰减系数（推荐 0.01） |

### 6.2 私有数据存储

- **数据根目录**：`data/plugins/astrbot_plugin_livingmemory/`
  - `conversations.db`：原始群聊消息与对话序列归档库（SQLite，由被动过滤器维护）。
  - `livingmemory.db`：长期记忆实体、语义文档与关联图谱库。
  - `vec_db/`：基于 FAISS 的向量索引文件。
- **数据安全**：包含真实群友历史发言，严禁纳入公共 Git 提交；定期通过 AstrBot 备份机制备份 SQLite 文件。

---

## 7. 本地开发与测试

### 7.1 开发依赖

- Python `>= 3.10`
- `aiosqlite`（异步 SQLite 支持，被动捕获与存储核心依赖）
- `jieba`（中文分词）
- `pytest`、`pytest-asyncio`

### 7.2 运行回归测试

在 `workspaces/plugins/livingmemory-production` 目录下执行定制模块测试：

```powershell
python -B -m pytest tests/test_archive_sampling.py tests/test_chat_events_tool.py tests/test_time_scoped_events.py tests/test_conversation_store.py tests/test_group_capture_filter.py tests/test_memory_scope.py -q
```

- **运行前提示**：测试套件依赖本地真实安装的 `aiosqlite` 库。若在未安装完整依赖的纯净开发环境中执行，测试桩将无法代理真实的异步连接方法；请在配置好依赖的虚拟环境或容器镜像中运行全量测试。

---

## 8. 打包与发布指引

统一使用项目根目录运维脚本进行制品构建与发布：

### 8.1 打包插件 ZIP

在 `bot_shushu` 项目根目录执行：

```powershell
python scripts/astrbot_plugin_package.py livingmemory-production --output dist/astrbot_plugin_livingmemory.zip
```
该命令会自动依据元数据规则将插件源码打包为符合规范的标准 ZIP 压缩包。

### 8.2 原生 HTTP 发布与更新

通过标准 HTTP 接口上传更新至正在运行的 AstrBot 实例：

```powershell
python scripts/astrbot_plugin_http.py --base-url http://192.168.1.100:6185 --profile dashboard update dist/astrbot_plugin_livingmemory.zip
```
- 发布完成后，AstrBot 将自动完成重载并加载新版记忆捕获与检索模块。
