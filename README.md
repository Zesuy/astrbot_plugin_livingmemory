# LivingMemory 生产定制版 v2.9.1

## 1. 项目名称与定位

**LivingMemory（`astrbot_plugin_livingmemory`）** 是面向 AstrBot 高频群聊的统一上下文与记忆引擎：基于上游 2.9.0，整合旁听滑窗、群友立体人物档案（LivingDossier）、客观长期记忆、真实聊天流水检索与即时控制台观测，承担替代 ChatPlus 与 Self Learning 的统一能力。

旁听消息先进入按群隔离的进程内滑窗，模型被唤醒时读取近况；需要回查细节时，再按需调用工具读取原文。滑窗本身没有数据库或模型 I/O，采用微秒级内存路径的设计，实际耗时以运行环境实测为准。后台记忆提炼、人物档案抽取仍会调用配置的记忆模型。

本文描述源码架构与配置，版本以 [metadata.yaml](metadata.yaml) 为准。生产状态以主项目 [任务记录](../../../docs/tasks.md) 为准：2026-11-04 记录已完成 2.9.0 上线及 ChatPlus 过渡；Self Learning 的停用仍列为待办，不能由替代能力推断其已停用。

## 2. 上游来源与演进血统

- **官方上游**：lxfight 的 [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)，沿用 **AGPL-3.0**，见 [LICENSE](LICENSE)。
- **当前基线**：上游 **2.9.0**。基线移植保留生产定制能力，完成 13 处核心冲突合并，兼容事务所有权与 `adapter_id` 隔离；引入原文档案、分层采样、事件提取及外部插件发送后归档 API。
- **当前生产分支**：`feat/unified-context-memory`。元数据版本保持 `2.9.0`，并不意味着代码等同于上游同版本；复现或回退还须记录分支、提交及包哈希。
- **主要演进**：`5a6f8c0`（2.9.0 移植）、`0bc65a1`（旁听与纯净注入）、`9fda089` / `cd8d02d`（记忆门禁与治理）、`3b6cc64` / `ecff415`（原文检索）、`3a50325` / `e6ef42f`（档案与乱序保护）。详细任务与来源分别维护于主项目 [tasks.md](../../../docs/tasks.md) 与 [sources.md](../../../docs/sources.md)。

生产线采用轻量注入和工具检索，不引入研究分支 `81d5fd4` 的重型分面导航。研究源码、上游参考与生产源码分别维护。

## 3. 为什么做深度定制

| 真实痛点 | 工程决策与结果 |
| --- | --- |
| 旧记忆混入闲聊、Bot 认错与无长期价值片段，回答时生搬硬套 | 删除截取 200 字原文强行入库的兜底；事件摘要接受空事件，拒绝 `quality=low`。以 `processed_no_memory` 表示正常处理但不生成记忆，推进处理游标并跳过 documents、atoms、图谱及向量写入。历史数据用审计脚本可逆软隔离，含正事的 `mixed_content` 不自动隔离。 |
| 群友身份不稳定，多人描述相互串用，旧别名纠正后又复活 | 在 `conversations.db` 中新增 `user_profiles` 单表，以 `(scope_id, user_id)` 主键隔离；硬事实绑定目标本人原文，身份更正单独验证目标。单值槽覆盖、集合槽增删，删除保留有界墓碑时间戳，阻止旧批次复活已删除事实或别名。 |
| 提示词充斥免责套话，记忆片段反复污染历史 | 用 `DialogueBuffer` 提供自然近况，`CleanContextInjector` 组合口语化片段；`TextPart.mark_as_temp()` 只附加当前请求，保存历史时过滤，同时清理旧版注入残留。 |
| 把刚才的玩笑夸大成“一贯如此”，把讨论计划说成已发生事实 | 标题改为 `[长期记忆与历史事件]`，附加三条证据边界；事件带来源时间、发言者与类型，长期印象只能依据档案及历史证据。 |
| 历史细节无法查证，只能依赖模糊摘要 | 默认开启原文归档，通过 `query_chat_events` 按关键词、时间和人物查询。此次生产架构背景包含约 4.3 万条聊天流水的检索接入；这是历史数据规模说明，不是随包数据或实时条数承诺。 |
| 注入过程黑盒化，无法判断模型当轮看到了什么 | `_inject_clean_context` 在 INFO 级记录会话、字符数及完整本轮注入正文；空注入仅记 DEBUG，可直接检查近况、档案和历史证据的实际组合。 |

三条证据边界的含义是：评价长期特征要依据档案与长期历史；近况只能描述为“刚才／今天说过”，不能扩张成“每天／总是”；长期依据不足时自然说明范围，只谈当前观察，不编造往事。当前代码在存在群聊近况时附加这组准则。

## 4. 核心功能与架构分工

### 非目标机器人指令守卫

在 LLM 请求钩子的最前端识别完整的第三方股票指令（如 `/图 D005`、`/大盘`、`/平多 D005 100`），命中后记录 INFO 日志并调用 `stop_event()`，不发送消息，也不进入当轮记忆召回。去除本机器人首尾结构化 @ 后再匹配，不改写原始事件或请求。

匹配仅接受明确的命令名及 ASCII 参数；无斜杠交易限定证券代码与数量格式（如 `买100股004`、`做多 D005 100`）。正常讨论、AstrBot 自身命令、混合图片/引用/其他用户 @ 和不明确的中文参数保持原有处理路径。此规则偏向低误杀，不试图识别所有股票机器人的命令方言。

### 4.1 第一层：轻量注入

```text
群消息 → 权限与会话开关校验 → 旁听内存滑窗 / 原文归档队列
                                       ↓
模型唤醒 → 当前群近况 + 当前说话人档案 + 自动召回长期记忆
                                       ↓
                    CleanContextInjector → 当前请求临时片段
```

推荐采用以下轻量档；其中配置数量与代码硬预算应分别理解：

| 注入内容 | 推荐规模与实际边界 |
| --- | --- |
| `[群聊近况]` | 推荐 `dialogue_buffer.max_messages=30`，即最多 30 条；源码默认 **50**，允许 **10–100** 条，按会话 FIFO 保存、LRU 淘汰，排除当前消息避免重复。单条捕获文本最多 2000 字符，重启后滑窗清空。 |
| `[说话人档案: …]` | 只读取当前说话人 **1 人**。硬事实与首选称呼、真名、别名合计最多 **12 项**，值文本合计最多 **200 字符**；标题、标签和分隔符不计入该预算。 |
| `[长期记忆与历史事件]` | 默认 `recall_engine.top_k=5`，即最多 **5 篇**候选；数量可配置。格式器每篇最多 **800 字符**，历史条目共享 **6000 字符**预算，包含条目前缀与换行预算；这不是整个请求的总字符上限。 |
| 当前称呼更正与说话人 | 本轮有明确更正时附加 `[本轮称呼更正]`，优先自然改口；有内容时附加当前说话人。 |
| 证据边界 | 有近况时附加三条准则，区分当堂观察与长期事实。 |

`DialogueBuffer` 不执行 LLM 请求；被动捕获钩子不主动唤醒主聊天模型。已有唤醒规则、其他插件和后台提炼仍有各自的运行路径。档案后台提取不阻塞当轮回复，新结果供后续轮次读取。

### 4.2 第二层：工具按需深翻

`query_chat_events` 只在调用时检索当前会话原文，不把整库直接注入提示词。工具有权限检查、`adapter_id` 隔离和覆盖／缺口信息；没有归档或查询不可用时应说明证据不足。

| `mode` | 使用场景 | 机制与边界 |
| --- | --- | --- |
| `overview` | 日报、周报、补看错过的讨论，尚不清楚话题 | 先按时间窗分层抽取讨论片段并返回密度；人物参数用于活动时间线，不按作者过滤。样本不是完整统计。 |
| `style_sample` | 模仿某位群友、替其拟回复 | 按人物取本人原生 Plain 文本，引用另列，过滤命令与非文本，跨时间取样并去重。 |
| `lookup` | 已知话题的精准核对 | 组合关键词、时段与可选人物，不用猜测关键词替代开放式概览。 |
| `time_read` | 发现线索后顺着某段讨论补读 | 按明确时间窗读取连续对话，可指定作者；利用返回游标继续读。 |
| `read` | 展开被截断的长消息 | 使用返回的 `evidence_handle`，必要时继续 `next_text_handle`。 |

参数示例（模型工具参数，不是聊天命令）：

```json
{"query":"昨天我说过哪些关于备份的话？","mode":"lookup","keywords":["备份"],"time_range":"昨天","person":"我","person_role":"speaker"}
```

- `query` 保留用户原始问题；问某人“说过什么”须用 `person_role=speaker`，默认 `about` 指被讨论者。
- `person=我` 绑定当前提问者，`person=你` 绑定机器人；无法唯一解析的昵称只能当线索，不能当身份确认。
- 时间锚定 `Asia/Shanghai`：今天为当前自然日；昨晚为昨日 18:00 至今日 06:00（不超过当前时刻）；刚才／刚刚为最近 30 分钟。
- `start`、`end` 是带时区的 ISO 8601 起止点，左闭右开，成对填写，不能与 `time_range` 同传。后续分页使用原返回 `cursor`。
- 不发言区间不能证明离线或未读；机器人既往猜测、引用与关系线索也不能直接证明事实。证据足够即可停止检索。

其他基础工具包括 `recall_long_term_memory`（检索压缩长期记忆及关系线索）、`memorize_long_term_memory`（主动记录重要事实）。具体工具注册受功能开关和运行状态约束，原文核对与长期记忆召回各有用途。

### 4.3 管理入口（需要管理员权限）

```text
/lmem status        # 当前记忆与会话状态
/lmem summarize     # 管理员手动触发当前会话提炼
/lmem search <词>   # 检查记忆检索效果
```

控制台出现“本轮记忆注入就绪”时可查看完整临时片段。日志证明当轮组织的上下文，不等于回答已正确使用全部证据。

## 5. 关键算法与核心代码模块索引

| 模块 | 职责 |
| --- | --- |
| [core/dialogue_buffer.py](core/dialogue_buffer.py) | 有界进程内滑窗、消息 ID 去重、当前消息排除、分钟时间标记、会话 LRU。 |
| [core/user_profile.py](core/user_profile.py) | 硬事实与身份操作归并、原文引句核验、单值／集合槽、事实预算、墓碑与称呼格式化。 |
| [core/user_profile_runtime.py](core/user_profile_runtime.py) / [storage/user_profile_store.py](storage/user_profile_store.py) | 后台有界队列、增量水位、目标身份绑定、短事务写入、`revision` 乐观锁、归档 epoch 防旧任务写回。 |
| [core/clean_context.py](core/clean_context.py) | 自然语言上下文组合、长期条目字符预算、称呼更正与证据边界。 |
| [core/event_handler_modules/memory_recall.py](core/event_handler_modules/memory_recall.py) | 唤醒时召回与注入、档案有效性检查、临时片段去重与清理、INFO 正文日志。 |
| [core/tools/chat_events_tool.py](core/tools/chat_events_tool.py) | `query_chat_events` 五模式入口、参数契约、当前用户绑定、授权及超时处理。 |
| [core/managers/archive_sampling.py](core/managers/archive_sampling.py) | 时段概览与风格采样：8 层时间桶、桶内消息序号取样；风格样本隔离引用、限制短时密集样本、去重。 |
| [core/processors/memory_processor.py](core/processors/memory_processor.py) | 读取当前摘要提示词，事件规范化、长期价值与质量门禁、`processed_no_memory` 契约。 |
| [core/processors/memory_processor_build.py](core/processors/memory_processor_build.py) | 构建客观摘要与原子记忆，删除原文兜底；低质量或正常空记忆不生成 atoms。 |
| [scripts/audit_dirty_memories.py](scripts/audit_dirty_memories.py) | 历史脏记忆扫描、混合内容保护、可逆软隔离与精确回滚；须在备份及范围核对后使用。 |

LivingDossier 硬事实只接受目标本人有来源支持的自述。称呼／身份更正允许另行绑定目标；邻近上下文可辅助理解指代，但不能冒充本人硬事实证据。默认同一群友最短更新间隔 300 秒，到期后下一条有效消息触发一批最多 40 条归档。失败不推进提取水位，清除群归档时级联清除档案并阻断此前在途任务。

`user_profiles` 不继承长期记忆全局共享规则。单值如 `current_main_game`、`device_env` 按原消息时间覆盖；集合如 `hobbies`、`key_milestones` 支持 `add` / `remove`。身份支持首选称呼、真名、别名及明确纠错；语义歧义仍需自然确认，不能把模型判断当作身份权威。

## 6. 配置指南

配置通过 AstrBot 插件配置页管理，完整字段见 [_conf_schema.json](_conf_schema.json)。以下为源码默认值；已有生产配置不会因 README 推荐值自动改变。

| 配置键 | 默认值 | 配置说明 |
| --- | --- | --- |
| `dialogue_buffer.enabled` | `true` | 开启旁听近况。 |
| `dialogue_buffer.max_messages` | `50` | 保留及注入 10–100 条；轻量档建议 `30`。 |
| `user_profiles.enabled` | `false` | 档案需显式开启，并同时开启原文归档；会产生后台记忆模型调用。 |
| `user_profiles.bot_ids` | 空文本 | 填已知群管等机器人账号，空格、逗号或换行分隔；自身账号始终排除。 |
| `user_profiles.update_interval_seconds` | `300` | 每个群友最短更新间隔，`0` 表示不限制。 |
| `archive.enabled` | `true` | 默认保存原文；允许显式关闭，影响后续捕获与档案功能。 |
| `archive.query_enabled` | `true` | 向模型开放原文检索工具。 |
| `archive.capture_mode` | `inherit` | 继承全量捕获开关；另有 `off`、`permitted_all`，仍须通过访问检查。 |
| `archive.result_chars` | `24000` | 单次工具结果原文字符上限。 |
| `archive.max_candidates` | `400` | 话题检索候选上限。 |
| `archive.capture_queue_size` | `512` | 原文捕获队列容量。 |
| `session_manager.enable_full_group_capture` | `true` | 捕获非唤醒群消息，配合归档模式使用。 |
| `recall_engine.top_k` | `5` | 自动召回数量，允许 0–50；`0` 跳过自动长期召回，近况和档案有各自开关。 |
| `recall_engine.max_k` | `10` | 主动长期检索单次数量上限。 |
| `recall_engine.injection_method` | `extra_user_content` | 唯一支持的临时注入方式；旧方式自动回退。 |
| `recall_engine.auto_remove_injected` | `true` | 清除旧注入方式残留。 |
| `provider_settings.embedding_provider_id` | 空文本 | 建议显式选择已启用的 Embedding Provider，避免回退时选错模型或初始化失败。 |
| `provider_settings.llm_provider_id` | 空文本 | 后台总结与档案抽取模型；空值使用 AstrBot 默认。 |
| `access_control.whitelist_enabled` | `false` | 开启后仅允许白名单；名单为空时拒绝记忆操作。 |

推荐组合为：近况 30 条、自动召回 5 篇、按需原文检索、显式启用档案并排除其他 Bot。模型、权限和归档准备就绪后再启用后台档案抽取。

原文归档和人物档案位于插件私有数据目录的 `conversations.db`；长期记忆、图谱与向量索引由插件数据目录管理。部署路径以 AstrBot 实际解析结果为准。归档、摘要及索引均属于业务私密数据，不能打进源码包或提交到 Git。

## 7. 本地开发与测试命令

建议使用 Python **3.12+** 的独立虚拟环境，并与目标 AstrBot API 保持兼容。运行时依赖见 [requirements.txt](requirements.txt)；测试需要 `pytest`、`pytest-asyncio` 及真实 `aiosqlite`，不能用缺失依赖时的测试桩代替数据库验证。

在本插件目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt pytest pytest-asyncio

# 旁听、临时注入与人物档案
python -B -m pytest tests/test_dialogue_buffer.py tests/test_event_handler_injection.py tests/test_user_profile.py tests/test_user_profile_identity.py -q

# 原文工具、采样、时间与权限隔离
python -B -m pytest tests/test_chat_events_tool.py tests/test_archive_sampling.py tests/test_time_scoped_events.py tests/test_conversation_store.py tests/test_group_capture_filter.py tests/test_memory_scope.py -q

# 记忆准入与处理回归
python -B -m pytest tests/test_memory_admission.py tests/test_memory_processor.py tests/test_memory_processor_regressions.py -q
```

需要全面回归时执行 `python -B -m pytest tests -q`；具体依赖还须与测试环境及宿主版本对齐。以上为可执行入口，不代表本轮已运行通过，也不将历史测试数量写作当前验收结果。涉及真实样本的验证仅使用经授权的隔离数据，不保存原始私密日志。

## 8. 部署与安全约定

部署统一遵循主项目 [维护流程](../../../docs/maintenance.md)：本地修改与核验后生成标准 ZIP，再交由 AstrBot 原生安装／同名更新接口管理生命周期。以下在 `bot_shushu` 根目录执行，仅示例本地打包：

```powershell
python -B scripts/astrbot_plugin_package.py livingmemory-production --output deploy/packages/livingmemory-2.9.0-release
```

`--output` 为制品目录，不能把 ZIP 文件名当目录参数。安装时使用该目录实际生成的 ZIP，并按维护流程核对插件标识 `astrbot_plugin_livingmemory`、版本 `2.9.0`、激活／加载与失败状态。具体接口参数、凭据读取和回退流程只在维护文档维护。

- 发布前核对现场基线、定制提交、变更范围、备份和回退包，发现差异先处理；同版本不同提交不能仅凭版本号判定一致。
- 禁止 SSH 逐文件读改或上传源码，禁止随包安装器、shell、runner 或自制生命周期；上传不额外 reload，不先卸载删数据，不忽略版本要求，写请求超时不盲目重传。
- 代码回退不得用旧数据库覆盖新增业务数据；schema 或依赖变化单独评估。备份应包含一致的数据库与相应索引，不能直接复制活动库文件冒充一致快照。
- 源码与发布包不得含真实密钥、QQ 登录态、群聊数据库、个人私密信息或原始私密日志。INFO 注入正文日志本身可能含群聊与档案内容，按私密业务数据控制访问和留存，不把它附入公共问题或提交。
- 设备操作前读取 `Local_network` 对应文档并确认 SSH MCP profile；6100u 为应用主机，w6100u 为网关。生产容器 `astrbot` 未经确认不得停止或修改配置，严禁向真实 QQ 发送测试消息。
- 隔离设备测试仅使用专用 `astrbot-livingmemory-test` 容器，不接入真实 QQ，测试后及时清理并停止。本地文档或磁盘版本更新不代表生产业务验收完成。
