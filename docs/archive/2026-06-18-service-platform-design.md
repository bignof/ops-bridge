# service-platform 设计（独立插件分发 + 运维平台）v3

> 诉求3 落地形态。取代现 NocoBase `@orchisky/plugin-service-hub` 的**全部平台职责**,并因**原生对接 service-hub** 做得更好。配套:本仓库 `service-hub`(agent 控制面)、`service-agent`、`2026-06-18-zero-downtime-rolling-restart-design.md`。
>
> **版本**:v1(薄 MVP)→ v2(完整平台,并入第一轮评审 47 confirmed)→ **v3(并入第二轮复审 32 confirmed:修净 download IDOR/迁移 version 重导;校正三键模型与现库不符、单活改 app 维护列、version 非空不变式、hub 隔离落地、feign 实为 9 个)**。

## Goal

独立于 NocoBase 的插件分发 + 运维平台:功能**不少于**现平台,并利用原生对接 service-hub 做得**更好**(单一事实源、准实时状态、零中断发布上线、单宿主机多实例感知)。账户/权限保持简单(单 admin)。

## 现状与动机

- 现平台 = NocoBase `@orchisky/plugin-service-hub`:UI 是 NocoBase schema 驱动页面(`src/client/index.tsx` 仅 22 行、collections/ 仅 `.gitkeep`,7 个 CRUD 页持久化在**运行库 uiSchemas 表**,插件代码里零 React 源码);后端 = UI 动态 collection 台账 + action + 经 feign 代理外部 Python `service-hub`。真正大脑在 service-hub,已验证扎实。
- **平台台账库已确认**:`nocobase_hub @ 192.168.0.30:3306`(49 playground,`/data/nocobase-hub/.env.prod`,可自由读;**与 58 客户库无关**)。
- 驱动(用户确认):①去 NocoBase 耦合 ②运维好用 ③功能不少于现平台、且因原生更好。账户/权限可简单。
- 安全约束:service-hub `dispatch`=对全机群 RCE,绝不与人/浏览器面同处一服务 → 独立 platform,hub 纯机器面、`HUB_ADMIN_TOKEN` 只存 platform 服务端。

## 原生对接带来的提升（据实，已校正夸大项）

| 现平台(隔 NocoBase) | service-platform(原生) | 校正 |
| --- | --- | --- |
| 命名空间**持久化 agentKey**(online/heartbeat 现状已是 list/get 实时拼 hub) | 连 agentKey 也不存:online/heartbeat 实时读 hub;密钥 **show-once** | online/heartbeat 非新 delta;真正去掉的是 agentKey 落库 |
| `t_comment_record` 抄存命令 + **1 分钟 xxljob 轮询**(滞后) | 命令历史/状态**实时读 hub**,砍本地表 + 砍 xxljob | hub commands 是本地表无损超集 |
| 「发布 → 去服务**普通 restart**=有宕机」 | 「发布 → **一键无感上线**」=发布→滚动重启→节点重启重拉新插件→**0 宕机** | **单宿主机多实例**范围;跨宿主机 fan-out=P2(见「滚动作用域」) |
| 命令/滚动靠前端轮询;无实时日志 | **日志=实时 SSE**(hub 现成);命令/滚动=**准实时(轮询 hub 快照)** | hub 现仅日志有 SSE;命令/滚动 SSE 需改 hub=P2 前置 |

## 架构

```
  运维人员 ──HTTPS──> service-platform (新)
       ├─ 人类登录/会话 (单 admin + JWT, Authorization: Bearer, 免 CSRF)
       ├─ 台账 DB (复用机群 MySQL8 的独立库 service_platform) + 真 DB 约束(alembic)
       ├─ 插件包存储 (.tgz, 本地卷, 平台生成路径) + 分发下载端点(按不可变 id, 归属式鉴权)
       ├─ BFF: 服务端持 HUB_ADMIN_TOKEN 原生调 hub (人看不到 token)
       └─ 托管 SPA (React+Vite+antd, 从零重建)
              │ httpx REST(快照) + SSE relay(日志)
              ▼  (仅平台+agent 网络可达, 见部署隔离)
        service-hub (现, 不动): agents/keys/commands/rolling/logs
              │ WS
              ▼
        service-agent (各宿主机) ── docker;  nocobase 业务节点 ── sync-plugins.js 拉插件
```

- 新目录 `service-platform/`(与 hub/agent 并列);**前端是新引入的 Node/Vite/antd 子工程**(services-monorepo 现纯 Python,0 个 package.json/.tsx、Dockerfile 单阶段)。
- 后端 FastAPI:登录+会话、台账 CRUD、上传/发布/回滚、分发查询+包下载、BFF 原生代理 hub、SSE relay 日志、托管 SPA。
- 有状态:平台 DB + 本地卷包存储。SQLAlchemy + alembic。**不抄 hub 的 agent/命令状态**(实时读)。

## 数据模型（平台 DB = MySQL8 独立库 service_platform；greenfield 真建约束）

> **provenance(已实查 live DB,权威)**:旧 9 collection 是 NocoBase UI 动态表(`collections/` 仅 .gitkeep);真实 schema 已从 `nocobase_hub@49` 元表导出并入库:**`docs/collections.sql` + `docs/fields.sql` + `docs/uischemas.sql`**(= 迁移脚本/DDL/前端基线的权威来源)。下表是**新平台**目标模型(基于真实列裁剪),非旧表快照。
>
> **实查纠正(推翻评审 M-1 的代码推断)**:旧 `t_service` **确有 `dir`(目录)、`image`、`action`(命令,冗余字段)** 三列(2026-03 UI 加,供命令下发),**非"无 dir 列"**。无独立 `serviceName` 列(queryPlugin 的 serviceName=`name` 别名)。无 `nacosServiceName`(滚动用,**唯一真新增**)。`t_service_plugin_version.previousVersionId` 列**存在但是死字段**(只写不读),迁移丢弃。`t_plugin_version` 有 `version`+`url`+(冗余)namespaceId/serviceId。**打印助手 `t_print_version_update` 不在本平台 scope(用户明确)。**

**精简(vs 现状)**:① namespace 不存 online/heartbeat/agentKey;② 删 `t_comment_record`(命令实时读 hub);③ 删 spv.previousVersionId(死字段);④ namespace 加 `pullTokenHash`。**旧 9 collection → 新建 8 表 → 必迁 7 表**(comment_record 不迁)。

| 表 | 关键字段 | DB 约束 | 说明 |
| --- | --- | --- | --- |
| `namespace` | id, code(=agentId), displayName, **pullTokenHash** | UNIQUE(code) | online/心跳实时读 hub;agentKey 不存(show-once);pullTokenHash=该 ns 节点拉包凭据哈希 |
| `service` | id, namespaceId(fk), **serviceCode**, name(别名), **dir**(目录), **defaultImage**, **nacosServiceName**(新增,nullable) | UNIQUE(namespaceId, serviceCode) | serviceCode↔分发;name=服务别名;dir↔命令下发 target(**旧有**);defaultImage=旧 image;nacosServiceName↔滚动(**唯一新增**,运维填/P1 nullable)。旧 action 冗余字段不迁(命令动作下发时选) |
| `plugin` | id, code(npm 包名), displayName | UNIQUE(code) | 全局 |
| `plugin_version` | id, pluginId(fk), **version NOT NULL**, name | UNIQUE(pluginId, version) | **version 必须=.tgz 内 package.json.version 且恒非空**(见分发协议 M-2) |
| `plugin_attachment` | id, pluginVersionId(fk), filename, size, **storagePath**(平台生成) | — | storagePath=`<pluginId>/<versionId>/<sanitized>.tgz`,**不用客户端 filename 拼路径** |
| `service_plugin` | id, serviceId(fk), pluginId(fk) | UNIQUE(serviceId, pluginId) | 绑定 |
| `service_plugin_version`(spv) | id, servicePluginId, serviceId, pluginId, pluginVersionId, versionOrder, isActive(yes/no), isRolledBack(yes/no), publishTime, **spvActiveKey**(nullable) | **UNIQUE(spvActiveKey)** | 发布记录;单活靠 app 维护的 nullable unique 列(见下) |
| `fetch_record` | id, namespaceId, serviceId, pluginId, pluginVersionId, fetchDate, remark | — | 拉取审计 |

**单活约束(M-4 改法)**:照 hub `RollingTaskModel` 同款——**应用维护的 nullable unique 列** `spvActiveKey = (isActive=='yes' ? f"{serviceId}-{pluginId}" : NULL)`,普通 `UNIQUE(spvActiveKey)`(MySQL/SQLite 都允许多个 NULL、仅一个非空 → 每 (serviceId,pluginId) 至多一行 active;**可内存 sqlite 单测**,alembic 无需裸 DDL/生成列)。发布/回滚在 `SELECT ... FOR UPDATE` 锁 servicePlugin 行的单事务里:**先把同 key 全部置 no(并清 spvActiveKey),再置目标行 yes(并设 spvActiveKey)** —— 两步顺序是正确性前提;靠唯一约束兜底 + 捕 IntegrityError(不先查后插)。

**spv 链表/状态机不变式(可测命题)**:① versionOrder 在 servicePluginId 维度递增;② **首次发布**=同(serviceId,pluginId)全置 no + 新行 yes,versionOrder=max+1;③ **历史版本重新激活(updateVersion)**=激活某已存在历史行,不新建——**且清其 isRolledBack='no'**(M-6:否则该行永久被回滚候选跳过);④ **回滚**=当前行置 no+isRolledBack='yes',激活候选谓词 `versionOrder<当前 ∧ isRolledBack='no' ∧ isActive='no'` 里 versionOrder 最大者。

## 功能 / 页面（现平台全量 + 运维增强；不丢任何动作）

**配置**:① 命名空间(列表+**实时**在线/心跳、增改删、轮换密钥 show-once、轮换 pull token show-once);② 服务(列表 serviceCode/服务别名/dir(目录)/nacosServiceName、增改删、**通用命令下发 update 换镜像/restart 带 dir+image**、**一键无感滚动重启**[与命令下发并列]、历史命令[实时读 hub,按 dir 还原+处理分页]);③ 插件(全局增改删);④ 服务插件(绑定/解绑,级联选择)。

**发布**:⑤ 插件上传(.tgz→解析+匹配,契约见分发协议;版本/历史列表);⑥ 插件发布——**①首次发布 ②历史版本重新激活 ③回滚**三类动作分清(见 spv 不变式);spv 发布历史列表;**「发布并一键无感上线」**=发布后直接触发该服务滚动重启;⑦ 获取记录。

**运维(原生增强)**:⑧ 机群总览(实时在线/就绪);⑨ 一键无感重启+准实时进度;⑩ 实时日志(SSE);⑪ **滚动恢复**(hub 重启遗留 interrupted 滚动 → acknowledge 人工放行;漏它该服务永久 409 卡死)。

> **自动 CRUD 须逐资源手写**(现 NocoBase 自动给 list/get/create/update/destroy);明确补:namespace update/destroy、plugin_version list/get、spv list、fetch_record list、service_plugin list。

## 前端范围（P1 主成本，从零重建）

- 现 7 页是 NocoBase 拖块 schema 页,**无可移植 React 源码**;新平台手写整套中后台(列表/表单/弹窗/校验/分页/级联选择器)。
- **基线冻结(M-7)**:P1 首个交付物 = 从 `nocobase_hub@49` 导出 7 页 uiSchemas → 转**每页字段/列/动作/校验清单**作冻结基线(与下文 DB 勘察合并一次做);P1 验收门 = 逐页对照该基线无缺失。
- **UI 风格=仿照 NocoBase admin**(用户定):antd5 表格 + 「筛选/添加/刷新/设置齿轮」action bar、列表 + 抽屉(Drawer)表单、级联关联选择器(命名空间→服务→插件→版本),复刻现运维熟悉的交互。`uischemas.sql` 即每页字段/列/动作的精确规格,照它落 ProTable 列与表单项,**降低从零设计的风险**(目标视觉/字段已知,非自由发挥)。
- **选型压成本**:antd + ProComponents(ProTable/ProForm)贴近 NocoBase 表格观感。请求统一封装(Authorization header + 平台错误码 + 字段白名单)。
- **构建/CI 增量**:新增 `service-platform/web/`(Vite+React+antd);**多阶段 Dockerfile**(node 构建 SPA → 拷进 python 镜像静态目录,FastAPI StaticFiles 托管,**单容器**);CI 加前端 lane(install/lint/build + 产物缓存)。

## 分发协议（兼容现 sync-plugins.js + 修 IDOR/版本漂移）

- 现节点 `sync-plugins.js`(模式二):`GET {adminUrl}{apiPath}?namespace=X&service=Y`(默认 `/api/t_service_hub:queryPlugin`)+ `Authorization: Bearer <token>`,返回 `[{pluginName,version,url}]`(兼容 数组/`{data}`/`{data:{list}}`),curl 下载 url 的 .tgz,**按 `plugin.version` 与本地 package.json.version 严格相等判断跳过/更新**(`plugin.version || extractVersionFromUrl(url)`)。脚本可配置,不改脚本,改节点 config 指向 platform。
- **兼容端点**:`GET /api/distribution/plugins?namespace=&service=` → 同形 `[{pluginName, version, url}]`,`url=PLUGIN_DOWNLOAD_BASE_URL + /api/distribution/download/{attachmentId}`,写 fetch_record。
- **version 不变式(M-2,关键)**:id 化后 url 不含 `-<version>.tgz`,节点 `extractVersionFromUrl` 恒返回 null → **compat 端点 `version` 字段必须恒非空且=包内 package.json.version**(否则节点 else 分支无条件重下)。plugin_version.version 列 NOT NULL + 一条"version 非空"断言测试。
- **下载防穿越 + 归属式鉴权(B5+High-1,关键)**:`GET /api/distribution/download/{attachmentId}` 只收**不可变 id**,服务端反查 storagePath 流式返回(杜绝自由 path);**鉴权按归属**——由 pull token 反解其 namespace,校验 `attachmentId→pluginVersion→spv→service→namespace == token.namespace`,不符返 **404**(不泄露 id 存在性);download **不依赖 query namespace**。`plugins` 端点同样校验 query namespace==token.namespace(403)。**对抗测试**:① 持 ns A token 拉 ns B 的 attachmentId→404 ② `/download/../../x`→404。(可选:attachmentId 用 UUID/ULID 抗枚举。)
- **per-namespace pull token(B6)**:token 存 `namespace.pullTokenHash`(哈希),轮换 action(show-once + 旧立即失效,常量时间比较)。
- **上传契约(M8)**:只收 `.tgz` + 校验合法 npm pack(tar 内含 package.json)+ basename 白名单;**`version` 取自包内 package.json.version**(非文件名 split——旧平台用文件名 split 会得 `rc.xxx` 这类垃圾值,见迁移 High-2);插件匹配 `pluginCode LIKE '%/<name>'`,0/多命中报错;(pluginId,version) 查重;storagePath 平台生成。

## 鉴权（简单;Authorization header JWT 免 CSRF）

- **人类会话(M6)**:`POST /auth/login {username,password}`(常量时间比较 env 凭据)→ 返回 JWT(响应体);SPA 内存持有 + `Authorization: Bearer` 发 `/api/**`,**免 CSRF**,配 CSP 缓解 XSS。中间件守 `/api/**`(除 `/auth/login` 与分发端点)。
- **JWT 运维(L-4)**:TTL 保守(≤8h);SECRET ≥32B 随机、不入库/不进前端;**轮换 `PLATFORM_JWT_SECRET`=全体 JWT 失效**的应急 kill-switch(会登出自己,写进运维)。
- **分发**:per-namespace pull token(独立 auth 路径)。
- **hub token**:`HUB_ADMIN_TOKEN` 仅服务端;BFF 对 hub 响应**按字段白名单透传**,命令 output/日志类脱敏或仅受控页展示,严禁记录任何 token/agent_key,错误统一平台错误码。
- **敏感读 gate(B7)**:日志流/命令历史 relay 归"敏感读",与改类动作同等显式 gate(非随登录默认开放)。

## hub 集成（13 个对接点；据实标注实时性）

- httpx 调 hub REST(带 X-Admin-Token)。**对接点=现 feign 9 个 ApiType + 新增 4 个(rolling-restart POST、rolling-status GET、`acknowledge` POST、`logs/stream` SSE)=13**(L-1:现 feign 实为 9 个,其中 QUERY_ALL_COMMENTS/QUERY_COMMENT_EVENT 现为 dead;rolling 类只在已废弃的 cnp `feat/service-rolling-restart` 分支,不算"现有")。
- **实时性据实(M1)**:命令结果/滚动进度 hub 现**只有快照 GET → 准实时轮询**;**只有日志是 SSE**。给 hub 加命令/滚动 SSE/WS=**P2 前置(改 service-hub,跨仓库)**。
- **acknowledge(M7)**:hub 重启保留 active_key(故意),靠 acknowledge 释放;不接则"滚动中重启 hub"后该 (agent,service) 永久 409。纳入运维页「滚动恢复」。
- **hub 端点鉴权现状(B7/L)**:`GET /api/agents`、`GET /api/commands*`、`POST /api/agents/{id}/logs/stream` 现**零鉴权**;rolling 系列有 token。BFF 对 hub 一律带 token(读端点多余但无害);推动给 hub 这三类补 `_require_admin_token`(跨仓库改进项)。
- 日志 relay:platform 服务端连 hub `POST /agents/{id}/logs/stream`(SSE,透传 dir/tail/timestamps + X-Requested-By/Source)relay 给浏览器;nginx 关 proxy_buffering。

## 滚动作用域（H1：单宿主机多实例；跨主机=P2）

- hub `_run_rolling` 全程只对一个 agent_id;agent 只认本机 `docker ps` 容器;对不上号直接 failed。即**滚动=单 agent/单宿主机内多实例逐个滚**(一台机上 N 容器)。
- namespace=单 agent=单宿主机(code=agentId);service.belongsTo 单 namespace。跨多宿主机的逻辑服务会跨多 namespace,当前不表达。
- **跨 agent fan-out 编排**(platform 对该逻辑服务涉及的每个 agent 依次发起 rolling + 合并进度)=**P2 新增**;P1 spec 写明"滚动=单宿主机范围",不暗示跨主机 0 宕机。

## 数据迁移（B4：P1b 前置，否则切流量静默失败）

- 新平台空库 → queryPlugin 返空 → 节点拉不到/回滚断链。必须迁存量,不能只验空库。
- **schema 已就绪**:真实列见入库的 `docs/collections.sql` + `docs/fields.sql`,前端基线见 `docs/uischemas.sql`(均从 nocobase_hub@49 导出)。字段映射照此定,无需再连 49。
- **迁 7 表**:namespace/service/plugin/plugin_version/plugin_attachment/service_plugin/service_plugin_version(comment_record 不迁;fetch_record 审计可选)。清洗:剔 agentKey;删 previousVersionId;丢弃 plugin_version.url(改 storagePath);为新唯一索引去重;校正 isActive 单活 + 回填 spvActiveKey。
- **version 重导(High-2,关键)**:旧库 version 是文件名 split 派生(`-rc` 构建下得 `rc.xxx` ≠ disk `1.7.0-rc.xxx`);迁移**必须解包每个 .tgz 读 package.json.version 覆盖**,不能原样搬旧值——否则切流量后节点 disk 比对失配 → **全机群每次启动重下 + 首装重 pm enable**。
- **迁包字节(M-5)**:先核 live `t_plugin_attachments` 的实际存储后端(本地 file-manager vs S3-pro;CLAUDE.md 默认含 file-storage-s3-pro)——本地:剥 baseUrl 前缀重建盘路径 cpSync;S3:拉流。按结果分两路。
- **验收**:① 从现客户库导入后,老 namespace/service 的 `/api/distribution/plugins` 返回正确 active 版本 + 真节点拉到;② **同节点连续两次 sync,第二次必须全部跳过、0 下载、0 pm enable**(证明 version 重导正确);③ 回滚链可用。

## 部署（hub 隔离落地，非一句口号）

- `service-platform/` 多阶段镜像;compose 与 hub 同拉;平台库=机群 MySQL8 的独立 database `service_platform`;包存本地卷。
- **hub 隔离=可验收改造(M-3)**:hub 现 compose `ports: 8080:8080`(0.0.0.0)+ 无 networks + 只读/日志零鉴权 → **改 `127.0.0.1:8080:8080` 或给 hub+platform 加共享 internal docker network**(hub 不发布到宿主机公网面);**测试**:从其它网段直连 `hub:8080/api/agents` 必须连接被拒。platform 暴露给运维(内网/nginx)。
- env:`SERVICE_HUB_URL`、`HUB_ADMIN_TOKEN`、`DATABASE_URL=mysql+pymysql://.../service_platform`、`PLUGIN_STORAGE_DIR`、`PLUGIN_DOWNLOAD_BASE_URL`、`PLATFORM_ADMIN_USER/PASSWORD`、`PLATFORM_JWT_SECRET/TTL`。

## 安全总览（评审重点集中列）

1. RCE 控制面隔离:hub 不对人;HUB_ADMIN_TOKEN 仅服务端;**hub 隔离落地为 127.0.0.1/internal network + 网络对抗测试**(M-3) + 推动 hub 补只读鉴权。
2. 下载防穿越 + 归属式鉴权(B5+High-1):id 化 + token→ns 归属校验 + 404 + 对抗测试。
3. 分发越权(B6):pull token 绑 ns + 轮换。
4. DB 完整性(M3/M-4):真唯一约束 + app 维护 nullable unique active 列 + 事务 + 行锁;捕 IntegrityError。
5. 会话(M6/L-4):Authorization header JWT 免 CSRF;TTL≤8h;SECRET 轮换=kill-switch。
6. BFF 脱敏:字段白名单;严禁回显/记录任何 token;output/日志含敏感串需掩码或仅受控页(P2 relay 前钉死方案)。

## 分期（P1 据实=大工程）

- **P1a 平台本体(对等)**:后端骨架 + 登录(JWT) + 台账 CRUD(逐资源含 update/destroy/list/get) + 真 DB 约束(alembic) + 上传(解析/校验/落盘,version=package.json) + 发布/历史激活/回滚(单活 nullable unique 列 + 事务锁) + queryPlugin 兼容端点(version 非空) + id 化归属式鉴权下载 + per-ns pull token + **整套 SPA 7 页**(对照冻结基线) + 多阶段镜像/前端 CI。
- **P1b 存量迁移**:DESCRIBE 旧库定列 → 迁 7 表 + 包字节(分本地/S3)+ version 重导 + 迁移后验收(两次 sync 全跳过)。依赖 P1a 的 storagePath/分发端点定稿,排其后。
- **P2 原生增强**:通用命令下发+历史(实时读 hub) + 机群总览 + 一键无感重启+进度 + 发布并一键无感上线 + 实时日志(SSE) + 滚动恢复(acknowledge) + 给 hub 加命令/滚动 SSE(hub 仓) + 跨 agent fan-out 滚动。
- **P3 切流量 + 下线(拆两步带门)**:①切流量(逐节点 config 指向 platform,灰度顺序、**admin 接 default 最后切**,改 config+重启排进零中断窗口,回退判据);②下线(量化门:全节点连续 N 次拉包成功 + 与旧平台对账一致 → 停旧插件 + handleStatusJob)。并存期:发布只在新平台、旧平台只读(或双写/对账)。

## 非目标（本期不做）

- 多操作员账号/RBAC/SSO/审计完善(单 admin)。
- pull→push 主动下发(仍 pull;发布可一键触发滚动准实时上线)。
- 跨宿主机滚动(P2)。

## 过渡（用户决定:全押 platform）

- **Plan 3 的 NocoBase bolt-on(cnp `feat/service-rolling-restart` 3 提交)不 merge/不 push**,被 service-platform 取代(分支留参考,可删)。
- **ops-bridge PR #3(agent+hub)保留合入** —— platform 原生依赖它。
- 现 NocoBase `@orchisky/plugin-service-hub`:UI+台账+分发全部由 service-platform 取代;切换期可并存(节点指向其一),P3 下线。

## 测试

- 后端 pytest:登录(常量时间)+ 会话 401、台账 CRUD、**唯一约束 + app 维护 active 列单活不变量(内存 sqlite 可测)+ 并发幂等**、上传解析/version=package.json、发布/历史激活/回滚事务原子性 + 链表不变式(含 M-6 isRolledBack 重置)、queryPlugin 兼容响应 + **version 非空**、**下载路径穿越→404 + 跨 ns 拉别家 attachmentId→404**、**未持会话打非分发端点→401**、BFF token 注入+脱敏。
- 隔离对抗(M-3):其它网段直连 hub:8080 被拒。
- 迁移:导入脚本单测 + 迁移后验收(两次 sync 全跳过、0 下载)。
- 端到端(复用 **31.57 测试床**):①现平台流程对等(建命名空间→服务→插件→绑定→上传→发布→真节点拉到)②原生增强(发布并一键无感上线 → 压测 0 宕机;实时日志;命令 update 换镜像)。

## 功能对照（现 NocoBase 插件 → service-platform；含评审补漏）

| 现(资源:action) | service-platform | 备注 |
| --- | --- | --- |
| t_namespace create/list/get/**update/destroy** | namespace CRUD | create 调 hub CREATE_AGENT(key show-once);list/get 实时读 hub 填在线 |
| t_namespace credentialsRotate | 轮换密钥(show-once) | hub CREDENTIALS_ROTATE |
| (新) | 轮换 pull token(show-once) | per-ns 分发凭据 |
| t_service CRUD | service CRUD | serviceCode/name(别名)/dir(目录)/image 旧有;nacosServiceName 唯一新增;action 冗余字段不迁 |
| t_comment_record execute(**update/restart+dir+image**) | **通用命令下发** | 保留;实时读 hub 状态(砍本地表+xxljob) |
| t_comment_record refresh/retry | 命令状态/重试 | 直读 hub |
| (cnp 废弃分支,非现状) | 一键无感滚动重启 | **P2 新增**(hub 有端点,NocoBase 侧从未实现) |
| (hub 有,现漏) | 滚动恢复 acknowledge | interrupted 人工放行 |
| t_plugin CRUD | plugin CRUD | |
| t_service_plugin create/destroy/list | service_plugin 绑定 | UNIQUE |
| t_plugin_version create(上传)/list/get | 插件上传/版本历史 | 解析契约见上 |
| t_plugin_version/spv pluginPublisher | **①首次发布** | versionOrder+1 |
| t_service_plugin_version updateVersion | **②历史版本重新激活** | 激活已存在行 + 清 isRolledBack |
| t_service_hub pluginRollback | **③回滚** | versionOrder/isActive/isRolledBack(删 previousVersionId) |
| spv list | 发布历史 | 手写 |
| t_service_hub queryPlugin | 分发端点 + id 化下载 | 归属式鉴权 + version 非空 + fetch_record |
| fetch_record list | 获取记录 | 手写 |

## Open Questions

1. ✅ **已定**:平台 DB=复用机群 MySQL8 独立库 `service_platform`(单活=app 维护 nullable unique 列,非生成列;窗口逻辑改为 app 多步事务,见 spv 不变式)。
2. ✅ **已定**:包存=本地卷 + per-ns pull token(Bearer)下载。
3. **[P2 前置]** hub 命令/滚动是否加 SSE/WS(改 service-hub);P1 先轮询。
4. JWT 库选型(pyjwt/jose);本设计默认 Authorization header,无 CSRF/cookie 问题。
5. 节点切换节奏/灰度顺序(admin 最后)、并存期发布纪律。
6. ✅ **已完成**:旧表真实列 + uiSchemas 已导出入库(`docs/collections.sql`/`fields.sql`/`uischemas.sql`)。**P1b 仍须确认**:live 包存储后端(本地 file-manager / S3-pro)以定迁包脚本两路。

## Self-Review

- 覆盖驱动:去耦合 ✅;运维好用 ✅;功能不少于现平台 ✅(对照含 update/三类发布/acknowledge/自动 CRUD);原生更好 ✅(提升表已校正)。
- 物理正确:agentKey show-once ✅;实时性据实(只日志 SSE)✅;feign=9 已校正 ✅。
- 安全:下载归属式鉴权防 IDOR+穿越 ✅;pull token 绑 ns ✅;hub 隔离落地+对抗测试 ✅;真 DB 约束(可 sqlite 测)✅;免 CSRF + JWT kill-switch ✅;BFF 脱敏 ✅。
- 数据模型据实(实查 live DB 纠正评审):service 的 dir/image 为**旧有列**(非新增)、nacosServiceName 为唯一新增;previousVersionId 旧有但死字段(迁移丢);单活改 app 维护列(可测);version NOT NULL+=package.json;真实 schema dumps 已入库。
- 迁移:version 重导 + 包后端分路 + 两次-sync 验收;DESCRIBE 旧库为 P1b 前置。
- 范围据实:前端从零重建 + 构建/CI + 迁移均显式计量;P1 拆 P1a(本体)/P1b(迁移)。
- OQ:#1/#2 已定,#3 为 P2 前置、#6 为 P1b 前置;**P1a 不被任何 OQ 阻塞,可进 writing-plans**。
