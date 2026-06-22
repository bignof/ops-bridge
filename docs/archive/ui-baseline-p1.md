# 前端冻结基线 ui-baseline-p1（service-platform 控制台）

> **本文件性质**：service-platform P1-SPA（前端控制台）的**冻结基线**，是 P1-SPA 计划 Task 1-6 的字段/列/动作权威来源，也是 Task 7 验收门（逐页对照「无缺失」）的对照清单。**纯文档，不写代码**。
>
> **派生来源（已实读核对，非推断）**：
> 1. **列 / 表单字段 / 校验** ← 转写自 `docs/uiSchemas.sql`（大写 S）现 7 页 schema（共 780 行 schema 片段；解析提取 `x-collection-field` 字段引用 + `x-action` 动作节点）。
> 2. **字段中文标签 / 语义** ← `docs/fields.sql`（每张表 `uiSchema.title` + interface 类型）。表清单见 `docs/collections.sql`。
> 3. **动作 → 端点绑定** ← 实读 `cnp` 仓 `packages/plugins/@orchisky/plugin-service-hub/src/server/actions/*/index.ts`（每资源 `resourceManager.define` 注册块）+ 各 action 实现文件，再映射到 P1a 新端点（P1a 契约见 `docs/2026-06-19-service-platform-plan-p1a-backend.md`）。
>
> **字段命名约定**：本基线**一律记 camelCase**，与 P1a 契约对齐（P1a `*Out`/`*In` 模型经 `to_camel` 输出 camelCase；DB ORM 内部可能是 snake，但 API 契约以 camel 为准）。**旧 NocoBase schema 中的 snake / 拼写残留已在「字段名修正」列标注并去净**（如 `image`→`defaultImage`、`namesapceId`→`namespaceId`）。
>
> **列表统一信封**：所有 list 端点回 `{count, rows, page, pageSize, totalPage}`；ProTable `request` 把 `current`/`pageSize` 映射为后端 `page`/`pageSize`，读 `{ data: rows, total: count }`。

---

## 0. 全局：旧 NocoBase 动作 → P1a 端点映射总表（权威）

下表是从 `plugin-service-hub` 源码实读出的**全部** `resourceManager.define` 注册，逐一映射到 P1a 新端点。**绑定不在 `uiSchemas.sql` 里**（6 个自定义 action 在该 dump 中 grep=0，旧绑定存在未导出的 customRequests 表），故全部从源码 + P1a 契约推。

| 旧资源（NocoBase） | 旧 action（源码实证） | 源码文件 | 真实语义（实读） | P1a 新端点 |
| --- | --- | --- | --- | --- |
| `t_namespace` | `create` (middleware) | `namespace/create.ts` | 调 hub `CREATE_AGENT(agentId=namespaceCode)` 取 `agentKey` 写库；**响应含 agentKey（show-once）** | `POST /api/namespaces`（201，响应 `{…, agentKey}` 不入库） |
| `t_namespace` | `list` / `get` | `namespace/query.ts` | 列表 / 详情 | `GET /api/namespaces` / `GET /api/namespaces/{id}` |
| `t_namespace` | `credentialsRotate` | `namespace/update.ts` | 调 hub `CREDENTIALS_ROTATE(agent_id=namespaceCode)` 取新 `agentKey` 写库 | **`POST /api/namespaces/{id}/rotate-key`** → `{agentKey}`（show-once）<br>**`POST /api/namespaces/{id}/rotate-pull-token`** → `{pullToken}`（show-once，P1a 新拆）见 §1 备注 |
| `t_service_hub` | `pluginRollback` | `serviceHub/pluginRollback.ts` | 当前 active 版回滚到上一**未回滚**版本（当前置 `isActive=no,isRolledBack=yes`；上版置 `isActive=yes`） | `POST /api/releases/rollback`（body `{spvId}`） |
| `t_service_hub` | `queryPlugin` | `serviceHub/query.ts` | **节点侧分发**：按 `namespace`+`service` 查 active 版本返回下载 URL 数组，并**写 `t_fetch_records`** | `GET /api/distribution/plugins?namespace=&service=`（pull-token 鉴权，**非 UI 动作**，见 §8 P2/边界） |
| `t_service_plugin_version` | `pluginPublisher` | `servicePluginVersion/create.ts` | 发布：校验绑定存在 + 版本未发过（否则提示去历史版本），全灭活后新建 `isActive=yes` spv | `POST /api/releases/publish`（body `{serviceId,pluginId,pluginVersionId}`） |
| `t_service_plugin_version` | `updateVersion` | `servicePluginVersion/update.ts` | **重新激活历史版本**：把该 (pluginId,serviceId) 全部 spv 置 `isActive=no`，目标 `id` 置 `isActive=yes`+刷新 `publishTime`，返回「发布成功！」 | `POST /api/releases/reactivate`（body `{spvId}`）—— ✅ 语义已实读确认为 reactivate（原 brief TODO 已消解，见下） |
| `t_plugin_version` | `create` (middleware `createData`) | `pluginVersion/create.ts` | **上传**：解析附件 filename/title 取 version、按 `pluginCode LIKE %/<名>` 匹配插件（0/多命中 400）、版本查重（400） | `POST /api/plugin-versions/upload`（multipart file=.tgz） |
| `t_plugin_version` | `pluginPublisher` | `pluginVersion/create.ts` | 同 spv 的 `pluginPublisher`（从插件版本对象触发发布） | `POST /api/releases/publish` |
| `t_service_plugin` | `create` (middleware) | `servicePlugin/create.ts` | 校验 (pluginId,serviceId) 绑定不重复（400「请勿重复关联」） | `POST /api/service-plugins`（201，UNIQUE→409） |
| `t_comment_record` | `execute` / `retry` / `refresh` | `commentRecord/*.ts` | 命令下发 / 重试 / 刷新状态 | **全部 P2，不做**（见 §9） |

> **原 brief TODO 已核定**：`updateVersion` 的真实语义经实读 `servicePluginVersion/update.ts` 确认为「把目标历史 spv 行重新置为唯一 active」，即 **reactivate**，映射到 `POST /api/releases/reactivate`，**无遗留 TODO**。

**`t_plugin`（插件）无自定义 action**：标准 CRUD（NocoBase 自动挂 list/get/create/update/destroy），P1a 对应 `/api/plugins` 标准 CRUD。

---

## 登录页（LoginPage）

非现 7 页之一（NocoBase admin 自带登录），但 P1-SPA 需自建。

| 项 | 内容 |
| --- | --- |
| 表单字段 | `username`（必填）、`password`（必填） |
| 校验 | 两者非空；失败提示后端返回的错误（401 → 「用户名或密码错误」） |
| 动作 | 「登录」→ `POST /auth/login` → `{token}`；成功存 token（sessionStorage + context）后跳 `/` |
| 辅助端点 | `GET /auth/me`（取当前用户，用于刷新保活 / 顶栏展示） |
| 守卫 | 无 token 访问受保护路由 → 跳 `/login`；任意请求 401 → 清 token + 跳 `/login` |

---

## 1. 命名空间页（NamespacesPage）— resource `namespaces`（旧 `t_namespace`）

**uiSchemas 现页引用字段**：`namespaceCode`、`name`、`agentKey`、`online`、`lastHeartbeatAt`、`remote`。
**fields.sql 语义**：`namespaceCode`=命名空间编码、`name`=命名空间别名、`agentKey`（无业务 label，密钥明文）、`online`=是否在线(enum true/false)、`lastHeartbeatAt`=最近心跳时间。

### 列（list 列）
| 列（camelCase） | 中文标题 | 来源/说明 |
| --- | --- | --- |
| `code` | 命名空间编码 | P1a 主键标签；**namespace 列用 `code`**（旧 `namespaceCode` → P1a 规范为 `code`；`name` 空时回退 `code` 作稳定标签） |
| `name` | 命名空间别名 | 可空 |
| ~~`online`~~ | 是否在线 | **P2 占位**：列保留但显 `-`（实时状态需接 hub，P1 不接） |
| ~~`lastHeartbeatAt`~~ | 最近心跳时间 | **P2 占位**：列保留但显 `-` |

### 表单字段（添加 / 编辑 Drawer）
| 字段（camelCase） | 中文 | 校验 |
| --- | --- | --- |
| `code` | 命名空间编码 | **必填**，唯一（409→「编码已存在」） |
| `name` | 命名空间别名 | 选填 |

> `agentKey` **不是表单输入字段**：由后端 create 时调 hub 签发、show-once 返回，前端只读弹窗展示，不入库、不可编辑。

### 动作按钮 → 端点
| 按钮 | uiSchemas 实证 | P1a 端点 | 说明 |
| --- | --- | --- | --- |
| 添加 | `create` | `POST /api/namespaces` | **创建成功若响应含 `agentKey` → ShowOnceModal 展示明文 + 复制** |
| 编辑 | `update` (Action.Link) | `PATCH /api/namespaces/{id}` | |
| 删除 | `destroy` | `DELETE /api/namespaces/{id}`（204） | |
| 轮换密钥 | `customize:table:request` title「轮换密钥」 | `POST /api/namespaces/{id}/rotate-key` → `{agentKey}` | 旧 `credentialsRotate`；ShowOnceModal 展示返回明文 |
| 轮换 pull token | （旧 schema **无**此按钮，P1a 新增） | `POST /api/namespaces/{id}/rotate-pull-token` → `{pullToken}` | 旧平台只 1 个「轮换密钥」按钮（轮 agentKey）；P1a 把凭证拆成 agentKey + pull token 两条 show-once 端点，前端需补「轮换 pull token」按钮 |
| 筛选 / 刷新 | `filter` / `refresh` | 客户端工具条 | |

> **show-once 复制**：用 `copy-to-clipboard` 包（HTTP+内网 IP 下 `navigator.clipboard` 不可用，见根 CLAUDE.md）。

### P2 故意不做（本页）
- `online` / `lastHeartbeatAt` 实时列（接 hub 心跳）—— 列占位显 `-`，**不接** hub 拉取。
- 旧 schema 引用的 `remote` 字段 —— 与 hub 远程通道相关，P1 不做。

---

## 2. 服务页（ServicesPage）— resource `services`（旧 `t_service`）

**uiSchemas 现页引用字段**：`serviceCode`、`name`、`dir`、`image`、`assocNamespace`、`action`。
**fields.sql 语义**：`serviceCode`=服务编码、`name`=服务别名、`dir`=目录、`image`(无业务 label)、`assocNamespace`=命名空间(m2o→t_namespace,fk=namespaceId)、`action`=命令。

### 列
| 列（camelCase） | 中文 | 来源/说明 |
| --- | --- | --- |
| `namespaceCode` | 命名空间 | **后端 LEFT JOIN 回的可读名**，不客户端拼 id→名 |
| `serviceCode` | 服务编码 | |
| `name` | 服务别名 | |
| `dir` | 目录 | |
| `nacosServiceName` | Nacos 服务名 | P1a 新增字段（滚动部署用） |

### 表单字段（添加 / 编辑 Drawer）
| 字段（camelCase） | 中文 | 字段名修正 | 校验 |
| --- | --- | --- | --- |
| `namespaceId` | 命名空间 | — | **必填**；关联选择，选项来自 `GET /api/namespaces`（列表展示用 `code`） |
| `serviceCode` | 服务编码 | — | **必填**；UNIQUE(namespaceId, serviceCode)→409 |
| `name` | 服务别名 | — | 选填 |
| `dir` | 目录 | — | 选填 |
| `defaultImage` | 默认镜像 | **旧 `image` → `defaultImage`** | 选填（P1a 契约重命名） |
| `nacosServiceName` | Nacos 服务名 | **新增（旧 schema 无）** | 选填（滚动部署用） |

### 动作按钮 → 端点
| 按钮 | uiSchemas 实证 | P1a 端点 |
| --- | --- | --- |
| 添加 | `create` | `POST /api/services`（201，409 唯一冲突） |
| 编辑 | `update` | `PATCH /api/services/{id}` |
| 删除 | `destroy` | `DELETE /api/services/{id}`（204） |
| 筛选 / 刷新 | `filter` / `refresh` | 工具条 |

> **级联过滤**：服务可按 `?namespaceId=` 服务端过滤（P1a `services` list 支持）；若做「按命名空间筛选服务」，传 `?namespaceId=`，**勿客户端全量拉再 filter**。

### P2 故意不做（本页）
- `action`（命令）字段 + 任何「下发命令 / 重启服务」按钮 —— 旧 schema 中服务页含 `customize:popup`「重启」、`customize:form:request`「重启」等命令类动作，**全部 P2（hub-relay 命令下发），本页不建**。
- `assocCommentRecord`（命令记录关联）/「历史命令」抽屉 —— P2（`t_comment_record`，见 §9）。

---

## 3. 插件页（PluginsPage）— resource `plugins`（旧 `t_plugin`）

**uiSchemas 现页引用字段**：`pluginCode`、`name`。
**fields.sql 语义**：`pluginCode`=插件编码、`name`=插件别名。

### 列
| 列（camelCase） | 中文 |
| --- | --- |
| `code` | 插件编码（旧 `pluginCode` → P1a `/api/plugins` 规范为 `code`） |
| `name` | 插件别名 |

### 表单字段（添加 / 编辑 Drawer）
| 字段（camelCase） | 中文 | 校验 |
| --- | --- | --- |
| `code` | 插件编码 | **必填**，唯一→409 |
| `name` | 插件别名 | 选填 |

### 动作按钮 → 端点
| 按钮 | P1a 端点 | 说明 |
| --- | --- | --- |
| 添加 | `POST /api/plugins`（201，409） | `t_plugin` 无自定义 action，标准 CRUD |
| 编辑 | `PATCH /api/plugins/{id}` | |
| 删除 | `DELETE /api/plugins/{id}`（204） | |
| 筛选 / 刷新 | 工具条 | |

### P2 故意不做（本页）
- 无 P2 命令类动作（本表纯字典维护）。

---

## 4. 服务插件页（ServicePluginsPage）— resource `service-plugins`（旧 `t_service_plugin`）

服务 ↔ 插件的关联（绑定）表。
**uiSchemas 现页引用字段**：`assocNamespace`、`assocService`、`assocPlugin`、`assocPluginVersion`（关联选择）。
**fields.sql 语义**：`assocNamespace`(m2o→t_namespace,fk **`namesapceId`**——DB 拼写错误)、`assocService`(m2o→t_service,fk=serviceId)、`assocPlugin`(m2o→t_plugin,fk=pluginId)。

### 列
| 列（camelCase） | 中文 | 说明 |
| --- | --- | --- |
| `namespaceCode` | 命名空间 | 后端 LEFT JOIN 可读名 |
| `serviceCode` | 服务编码 | 后端 LEFT JOIN 可读名（P1a list 回 `serviceCode`/`pluginCode`） |
| `pluginCode` | 插件编码 | 后端 LEFT JOIN 可读名 |

### 表单字段（添加 Drawer，**三级级联**）
| 字段（camelCase） | 中文 | 字段名修正 | 校验 / 级联 |
| --- | --- | --- | --- |
| `namespaceId` | 命名空间 | — | **必填**；关联选择 `GET /api/namespaces` |
| `serviceId` | 服务 | **旧 fk 拼写 `namesapceId`，P1a 用正确 camelCase** | **必填**；选命名空间后按 `GET /api/services?namespaceId=` 服务端过滤拉服务 |
| `pluginId` | 插件 | — | **必填**；按 `GET /api/plugins`（必要时 `?…` 过滤）拉插件 |

> **唯一约束**：UNIQUE(serviceId, pluginId)→409（旧实现 400「所选插件已存在服务中，请勿重复关联」，P1a 规范化为 409）。
> **级联走服务端过滤**：选命名空间 → `list('services',{namespaceId})`（带 `?namespaceId=`）；**别纯客户端拉全量再猜**（手写 ProForm 无 NocoBase 关联选择的自动 filter magic）。

### 动作按钮 → 端点
| 按钮 | P1a 端点 | 说明 |
| --- | --- | --- |
| 添加 | `POST /api/service-plugins`（201，409） | 旧 `create` middleware 做重复校验 |
| 删除 | `DELETE /api/service-plugins/{id}`（204） | |
| 筛选 / 刷新 | 工具条 | |

> **本页无「编辑」**（关联表只增删，对齐 P1-SPA 计划 Task 3 字段表「仅 列表/添加/删除」）。

### P2 故意不做（本页）
- 任何命令下发 / 命令历史动作 —— 与命名空间页占位写法一致，P2 hub-relay 不建。

---

## 5. 插件上传页（PluginUploadPage）— resource `plugin-versions`（旧 `t_plugin_version` 的 upload）

上传 .tgz → 解析 → 落库为插件版本。
**uiSchemas 现页引用字段**：`assocPluginAttachments`（插件文件 Upload）、`version`、`filename`、`assocPlugin`、`assocService`、`assocNamespace`、`createdAt`。
**fields.sql 语义**：`version`=版本、`name`=名称、`url`、`assocPluginAttachments`=插件文件(hasOne→t_plugin_attachments)、`assocPlugin`=插件(m2o)。

### 上传区
| 项 | 内容 |
| --- | --- |
| 控件 | antd Upload 拖拽 / 选 `.tgz` |
| 动作 | 上传 → `POST /api/plugin-versions/upload`（multipart，字段 `file`） |
| 成功 | message 显示解析出的 `version`（响应 `{pluginVersionId, attachmentId, version}`） |
| 失败提示 | **413**（文件超限，P1a 上限如 200MB）；**400**（未匹配插件 / 匹配多个 → 「请手动选择要上传的插件」）；**409**（`(pluginId, version)` 版本已存在 → 「版本号已存在」） |

> 旧实现（`pluginVersion/create.ts`）：从附件 `filename`/`title` 解析 `version`（`title.split('-')` 末段）、按 `pluginCode LIKE %/<尾段>` 匹配插件、版本查重。P1a 改为解析 `.tgz` 内 `package.json` 取 `{name, version}`，按 `name` 匹配 `plugin.code`。

### 列表（下方 ProTable：已上传版本）
| 列（camelCase） | 中文 | 说明 |
| --- | --- | --- |
| `pluginCode` | 插件编码 | 后端 LEFT JOIN 可读名 |
| `version` | 版本 | |
| `filename` | 文件名 | 来自 attachment |

- 端点：`GET /api/plugin-versions?pluginId=`（服务端分页，信封 `{count,rows,…}`）。
- 可选 `?pluginId=` 过滤（按插件查其版本）。

### 动作按钮 → 端点
| 按钮 | P1a 端点 |
| --- | --- |
| 上传 | `POST /api/plugin-versions/upload` |
| 刷新 | 工具条（重拉 `GET /api/plugin-versions`） |

### P2 故意不做（本页）
- 「发布」入口若放在上传页（旧 `pluginVersion` 也注册了 `pluginPublisher`）：**发布动作统一在插件发布页（§6）做**，上传页只负责上传 + 列版本，避免双入口；上传页**不建**发布按钮（除非后续 Task 明确要求，留 §6 单点）。

---

## 6. 插件发布页（ReleasesPage）— resource `releases`（旧 `t_service_plugin_version`）

发布 + 历史版本 + 重新激活 + 回滚。**这是动作最密集的页**。
**uiSchemas 现页引用字段**：`assocService`、`assocPlugin`、`assocPluginVersion`、`assocPreviousVersion`、`publishTime`、`versionOrder`、`isRolledBack`。
**fields.sql 语义**：`isActive`=运行版本(enum yes/no)、`isRolledBack`=是否回滚过(enum yes/no)、`publishTime`=发布时间、`versionOrder`=版本序号、`assocPreviousVersion`=上一个版本(m2o→t_plugin_version,fk=previousVersionId)。

### 主表列（`GET /api/releases` 不传 filter → 每绑定当前 active 行）
| 列（camelCase） | 中文 | 说明 |
| --- | --- | --- |
| `namespaceCode` | 命名空间 | 后端 LEFT JOIN 可读名 |
| `serviceName` / `serviceCode` | 服务 | 后端 LEFT JOIN 可读名（P1a list 回 `serviceCode`） |
| `pluginCode` | 插件 | 后端 LEFT JOIN 可读名 |
| `version` | 当前版本 | 当前 active 版本号 |
| `publishTime` | 发布时间 | |
| `isActive` | 运行版本 | Tag 标色（yes=运行中） |
| `isRolledBack` | 是否回滚过 | Tag 标色 |

### 发布 Drawer（工具条「发布」开）
| 字段（camelCase） | 中文 | 级联 / 校验 |
| --- | --- | --- |
| `namespaceId` | 命名空间 | 关联选择 `GET /api/namespaces` |
| `serviceId` | 服务 | 选命名空间后 `GET /api/services?namespaceId=` 服务端过滤 |
| `pluginId` | 插件 | 选服务后 `GET /api/service-plugins?serviceId=` 取该服务已绑定插件 |
| `pluginVersionId` | 版本 | 选插件后 `GET /api/plugin-versions?pluginId=` 拉版本 |

> 级联链：命名空间 → 服务 → 插件 → 版本，**逐级服务端过滤**（`?namespaceId=`/`?serviceId=`/`?pluginId=`）。

### 历史版本抽屉（行操作「历史版本」开）
- 端点：`GET /api/releases?serviceId=&pluginId=`（取该 service+plugin 的**全部 spv 历史**，不止 active）。
- 列：`version` / `versionOrder` / `publishTime` / `isActive` / `isRolledBack`。
- 每行可「重新激活」（reactivate）。

### 动作按钮 → 端点（全部实证）
| 按钮 | uiSchemas 实证 | P1a 端点 | 说明 |
| --- | --- | --- | --- |
| 发布 | `create`/`customize:form:request`/`customize:table:request` title「发布」 | `POST /api/releases/publish`（body `{serviceId, pluginId, pluginVersionId}`） | 旧 `pluginPublisher`；版本已发过 → 提示去历史版本 |
| 历史版本 | `customize:popup` title「历史版本」 | `GET /api/releases?serviceId=&pluginId=` | 打开抽屉 |
| 重新激活 | （历史抽屉行内） | `POST /api/releases/reactivate`（body `{spvId}`） | 旧 `updateVersion`（实读=把目标历史 spv 置唯一 active），**非编辑** |
| 回滚 | `customize:table:request` title「回滚」 | `POST /api/releases/rollback`（body `{spvId}`） | 旧 `pluginRollback`；回滚到上一未回滚版本；非 active 版报「无需回滚」 |
| 筛选 / 刷新 | `filter` / `refresh` | 工具条 | |

> **主表不传 filter**（后端按 `isActive=yes` 回每绑定当前激活行）；历史抽屉传 `serviceId`+`pluginId`。**不要**自建 over-service_plugin 的聚合端点（P1a `GET /api/releases` 单端点服务两种视图）。

### P2 故意不做（本页）
- 「发布并一键无感上线」/ 滚动无感上线按钮 —— **P2（滚动重启）**，本期发布页只改 spv 状态机，**不建**无感上线按钮。
- 任何「重启服务」「下发命令」类按钮 —— P2 hub-relay。

---

## 7. 获取记录页（FetchRecordsPage）— resource `fetch-records`（旧 `t_fetch_records`）

**只读审计表**：记录由**节点侧分发端点**（`queryPlugin` / P1a `GET /api/distribution/plugins`）拉取插件时写入，**非 UI 创建**。
**uiSchemas 现页引用字段**：`assocNamespace`、`assocService`、`assocPlugin`、`assocPluginVersion`、`fetchDate`、`remark`。
**fields.sql 语义**：`fetchDate`=获取时间、`remark`=备注（旧实现存整条 plugin JSON）、各 assoc=m2o。

### 列（只读）
| 列（camelCase） | 中文 | 说明 |
| --- | --- | --- |
| `namespaceCode` | 命名空间 | 后端 LEFT JOIN 可读名 |
| `serviceName` / `serviceCode` | 服务 | 后端 LEFT JOIN 可读名（fields 标签「所属服务」） |
| `pluginCode` | 插件 | 后端 LEFT JOIN 可读名 |
| `version` | 插件版本 | 后端 LEFT JOIN 可读名 |
| `fetchDate` | 获取时间 | |
| `remark` | 备注 | 选展示 |

### 动作按钮 → 端点
| 按钮 | P1a 端点 | 说明 |
| --- | --- | --- |
| （无增删改） | — | 纯只读 |
| 筛选 | `GET /api/fetch-records?namespaceId=&serviceId=` | 可按命名空间 / 服务过滤 |
| 刷新 | 工具条 | 重拉 |

> **必须服务端分页**：`GET /api/fetch-records` 传 `page`/`pageSize`，信封 `{count,rows,…}`。此审计表行数无界，**勿全量返回**。

### P2 故意不做（本页）
- 无写操作；不做导出 / 清理等（P1 不在范围）。

---

## 8. 节点侧分发端点（非 UI 页面，登记防误建）

`queryPlugin`（旧 `t_service_hub:queryPlugin`）→ P1a `GET /api/distribution/plugins?namespace=&service=`：

- **pull-token 鉴权**（非用户会话 token），节点用 pull token 拉取该 namespace 下 active 插件。
- 返回**数组** `[{pluginName, version, url}]`（**这三字段不走 to_camel 改名，与现 `queryPlugin` 字面一致**）。
- 副作用：写一条 `t_fetch_records`（即 §7 获取记录的数据来源）。
- 下载：`GET /api/distribution/download/{attachmentId}`。

**这不是控制台的某个按钮**，控制台「发布主表」用 `GET /api/releases` 展示当前激活绑定；分发是节点带外消费。前端**不建**调 `/api/distribution/*` 的 UI 动作。

---

## 9. 全局 P2 故意不做项（逐页已分散标注，此处汇总防误建）

以下旧分发平台存在、但 **P1 故意不做**（多为 hub-relay / 命令通道 / 实时心跳，属 P2）。后续 subagent **勿误建**对应按钮：

| P2 项 | 旧实证（uiSchemas / 源码） | 归属页 | 说明 |
| --- | --- | --- | --- |
| 命令下发（execute） | `t_comment_record:execute`；schema `customize:form:request`「重启」 | 服务页 | hub-relay 下发命令到节点 |
| 命令重试（retry） | `t_comment_record:retry`；schema `customize:table:request`「重试」 | 命令历史 | |
| 命令状态刷新（refresh） | `t_comment_record:refresh`；schema `customize:table:request`「刷新状态」 | 服务 / 命令历史 | |
| 命令历史抽屉 | schema `customize:popup`「历史命令」；表 `t_comment_record`（命令记录：action/dir/status/requestId/executeTime） | 服务页 | 整张 `t_comment_record` P2 |
| 在线 / 心跳实时列 | `t_namespace.online` / `lastHeartbeatAt`；字段 `remote` | 命名空间页 | 列占位显 `-`，不接 hub |
| 「重启服务」按钮 | schema `customize:popup`「重启」、`customize:form:request`「重启」 | 服务页 | hub-relay |
| 发布并一键无感上线 | （滚动重启，spec 另列） | 插件发布页 | 滚动无感上线，P2 |

> **占位写法约定**：P2 列保留但显 `-`（如命名空间 online/心跳）；P2 动作**不建按钮**。与各页「P2 故意不做」小节一致。

---

## 10. 字段名 snake / 拼写残留修正清单（去净核对）

| 旧（NocoBase schema / DB） | P1a camelCase 契约 | 出现页 |
| --- | --- | --- |
| `image` | `defaultImage` | 服务页 |
| （无） | `nacosServiceName`（新增，滚动用） | 服务页 |
| `namesapceId`（DB fk 拼写错误） | `namespaceId` | 服务插件页（assocNamespace fk） |
| `namespaceCode`（旧字段名） | `code`（namespace 主标签）+ 列名 `namespaceCode`（JOIN 回的可读名） | 命名空间页 / 各 JOIN 列 |
| `pluginCode`（旧字段名） | `code`（plugin 主标签）+ 列名 `pluginCode`（JOIN 回的可读名） | 插件页 / 各 JOIN 列 |
| `serviceCode` | `serviceCode`（保留；service 主键标签仍 `serviceCode`） | 服务页 / 各 JOIN 列 |
| enum `isActive`/`isRolledBack` = `yes`/`no` | 保留 `yes`/`no`（P1a 内部布尔 `isActive`/`isRolledBack`，list 输出仍 camelCase） | 发布页 |

> **核对结论**：本基线所有表单字段 / 请求体字段已记为 camelCase，**无 snake 残留**；JOIN 回的只读列名（`namespaceCode`/`serviceCode`/`pluginCode`/`version`）虽含「Code」但本身即 camelCase，是 P1a 契约规定的可读名列，非待修正残留。

---

## 验收门（Task 7 Step 5b 用）

逐页对照本基线核对实现的 **列 / 表单字段 / 动作按钮 / 校验** 无缺失，尤其每个动作确实调到了本文件标注的 P1a `resource:action` / 端点：

- [ ] 登录页：username/password + `POST /auth/login`
- [ ] 命名空间页：列(code/name+P2占位) / 表单(code必填+name) / 动作(增删改+轮换密钥+轮换pull token) / show-once
- [ ] 服务页：列 / 表单(含 defaultImage/nacosServiceName) / 增删改 / `?namespaceId=` 级联 / 无命令按钮
- [ ] 插件页：列(code/name) / 表单(code必填) / 标准 CRUD
- [ ] 服务插件页：列(JOIN名) / 三级级联(namespace→service→plugin,服务端过滤) / 仅增删 / 409
- [ ] 插件上传页：Upload→`/upload`(413/400/409 提示) + 版本列表(`?pluginId=`服务端分页)
- [ ] 插件发布页：主表(`/releases`不传filter) + 发布Drawer(四级级联) + 历史抽屉(`?serviceId=&pluginId=`) + 重新激活/回滚 + Tag 标色
- [ ] 获取记录页：只读列(JOIN名) + 服务端分页(`/fetch-records`) + `?namespaceId=`/`?serviceId=` 过滤
- [ ] P2 边界：命令下发/重试/状态/历史、在线心跳列、无感上线 —— 逐页确认**未建**对应按钮
