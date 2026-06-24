# P2 / P5-5 书面方案 —— worker 改配置 + 老节点迁移(cnp + 部署侧)

> 状态(2026-06-24 更新):**P2 已实现 + 床上真机验证;P5-5 recipe 已床上验证,生产迁移待授权**。
> - P2 代码已落 cnp `feat/service-agent` commit `308538139b`(scoped,**未推**):`sync-plugins.js`(gate 去 `&& NAMESPACE` + 首装失败 fail-closed)+ `docker-entrypoint.sh`(`|| exit 1`)+ `.example`(agent 模式示例)。
> - 验证:①P2 三轮 e2e(node1 容器内,首装/版本跳过/坏url exit1 全过);②P5-5 真机:node1 真重启走真 entrypoint→真配置(无 ns)→新 gate→本机 agent→版本跳过→健康恢复(随后已还原 node1 到迁移前以避免最小 tgz 在共享卷的隐患)。
> - 前置 P1 已在 rolltest 床联调验收通过(见 [`plugin-distribution-dev-plan.zh-CN.md`](plugin-distribution-dev-plan.zh-CN.md) 「P1 验收」行)。
> - 落点:P2-1/P5-5 是**部署侧运行时文件**;P2-2/P2-3 改 **cnp 仓** `docker/nocobase/sync-plugins.js`。**真实生产迁移(P5-5 第 5 节)仍待授权执行**。

## 0. 现状(已读 cnp 代码,作为改动基线)

`docker/nocobase/sync-plugins.js`(容器启动 init,NocoBase 进程起来前同步插件):

- **模式一(直配)**:`CONFIG.plugins[]` 给死 url。
- **模式二(admin API)**:gate 在 `:353` `else if (ADMIN_URL && NAMESPACE && SERVICE)` → 拉 `${ADMIN_URL}${API_PATH}?namespace=&service=`,带 `Authorization: Bearer ${ADMIN_TOKEN}`(`:164`),默认 `API_PATH=/api/t_service_hub:queryPlugin`(`:97`)。
- **版本跳过**:`processPlugin` 用 `plugin.version || extractVersionFromUrl(url)`(`:312`)与本地 `storage/plugins/<pkg>/package.json` 比对,一致则跳过(`:318-323`)。
- **失败语义现状**(与 M4 对照):
  - 拉清单失败 → `warn + return`,**保留本地**(`:376-379`)✓
  - 清单为空 → `log + return`,**不清空**(`:380-383`)✓(但用 `log` 非 `warn`)
  - 单插件装失败 → `warn + failed++ + 继续`,**隔离不中断**(`:412-415`)✓
  - 主流程任何异常 → `exit 0`(`:436-439`)——**永不阻断 worker 启动**(这正是 P2-3 要决断的点)。

老节点配置形态(`sync-plugins.config.json.example`):`adminUrl: http://admin.orchisky.com` + `adminToken`(=对 admin 的 root-JWT)+ `apiPath: /api/plugin-admin:query` + `namespace` + `appName`。

新模型(P1 已落地)下 agent 的 worker-facing 端点:`GET /plugins?service=<svc>` → `[{pluginName, version, url}]`,`url` 指向 **agent 自己** `/download/{attachmentId}`(**tokenless**);agent **忽略** worker 传入的 namespace,恒用本机 `PLUGIN_NAMESPACE`。

---

## 1. ⭐ 关键前提决策:worker → 本机 agent 的网络可达

这是 P2 的**唯一真正开放点**,先定它,其余都是按它填值。agent 的插件服务默认绑 `PLUGIN_SERVE_HOST=127.0.0.1:18082`(P1-2 有意只 loopback,不复用 health 的 0.0.0.0)。但 cnp worker 与 agent 是**两个容器**,worker 的 `127.0.0.1` 不是 agent 的 `127.0.0.1`,跨容器够不着。三个选项:

| 选项 | agent 网络 | `PLUGIN_SERVE_HOST` | worker `adminUrl` | 评价 |
|---|---|---|---|---|
| **A(推荐)** | bridge + `ports: ["18082:18082"]` | `0.0.0.0` | `http://<宿主私网IP>:18082` | 改动最小(床现状即 bridge);worker 复用早已知的宿主 IP(WS_URL/NACOS 同款),零新信息;0.0.0.0 仅暴露在内网宿主网卡,合「内网不较真」取舍 |
| B | `network_mode: host` | `127.0.0.1` | `http://127.0.0.1:18082` **仅当 worker 也 host 网** | 最贴「本机 loopback」设计原意,但要求 **worker 也 host 网**;cnp worker 现在 bridge+publish(80→18029…),要全改 host 网,部署改动大 |
| C | bridge | `127.0.0.1` | `http://127.0.0.1:18082`(worker `network_mode: "container:<agent>"`) | worker 与 agent 共享 netns,loopback 直通且不暴露端口;但 worker 生命周期耦合 agent,且 worker 自己的端口发布要挪到 agent 容器,改动怪 |

**推荐 A**:agent 容器 `PLUGIN_SERVE_HOST=0.0.0.0` + 发布 `18082`,worker `adminUrl` 用宿主私网 IP。理由=部署最省事(steering 最高优先),且每个 worker 本就知道宿主 IP。**若安全口径要求严格 loopback-only**,再走 C(共享 netns)。本方案后续按 A 写值。

> 注:无论选哪个,**一宿主一 agent**,该宿主上所有 worker 的 `adminUrl` 都指这同一个本机 agent;agent 的 `PLUGIN_NAMESPACE` 决定这台机归属哪个 ns。

---

## 2. P2-1 —— worker `sync-plugins.config.json` 改写(部署侧文件,不在 cnp 仓)

把模式二从「指向 admin」改为「指向本机 agent、tokenless」。每个 worker 一份:

```jsonc
{
  "adminUrl": "http://<宿主私网IP>:18082",   // 本机 agent 的 worker-facing 端口(选项A)
  "apiPath": "/plugins",                       // 覆盖默认 /api/t_service_hub:queryPlugin
  "service": "<本 worker 的 service_code>",     // 必填,= console 台账里的 serviceCode
  "namespace": "<可留,agent 忽略其值>",         // 保留以兼容现 gate;P2-2 后可省
  "rejectUnauthorized": false
  // 不再有 adminToken —— agent /download 是 tokenless;回源 token 在 agent 端,不在 worker
}
```

要点:
- **去掉 `adminToken`**:worker→agent tokenless。原来 worker 持有的对 admin 的 root-JWT 整条退场(迁移见 P5-5)。
- **`apiPath: /plugins`**:agent 端点无 `/api` 前缀、非 nocobase `:action` 风格。最终请求 `http://<ip>:18082/plugins?namespace=&service=`,agent 只认 `service`。
- **`version` 字段链路**:agent 清单**带 `version`**(已验证),`processPlugin` 优先用它做跳过比对(`url` 是 `/download/{id}` 无版本号,`extractVersionFromUrl` 会回 null,但 `plugin.version` 兜住)。⇒ 重启不会每次重下,**前提是 agent 清单恒带 version**(P1 已满足)。
- **响应字段兼容**:agent 回 `pluginName`,脚本 `:375` 自动 `pluginName→packageName`,无需改脚本。

---

## 3. P2-2 —— `sync-plugins.js` mode2 gate(cnp 仓,必改)

新模型 worker 可**完全无 ns**(ns 归 agent)。但现 gate `:353` 硬要 `&& NAMESPACE`,若部署省略 namespace 字段则整个模式二不触发 → 静默不同步。**改为不强制 namespace**:

```diff
- else if (ADMIN_URL && NAMESPACE && SERVICE) {
+ else if (ADMIN_URL && SERVICE) {            // ns 归 agent;namespace 仅在有值时透传(agent 忽略)
```

URL 拼接 `:354` 的 `namespace=${encodeURIComponent(NAMESPACE)}` 在 NAMESPACE 为空时给 `namespace=`,agent 忽略,无害;**可不动**。若要更干净,按需省略空 ns 参数(评审 L3 的「按需省略 URL」),非必须。

> 兼容性:此改动对**老的 admin 模式无破坏**——老配置仍带 namespace,`ADMIN_URL && SERVICE` 同样成立。即「只放宽不收紧」。

---

## 4. P2-3 —— 失败语义硬化(补 M4)

现状已满足大半(见 §0)。需**决断 + 落地**的只有一条:

- **(a) 空清单 / 拉取失败 → 保留本地**:已满足(`:376-383`)。建议把空清单的 `log` 升为 `warn` 醒目,**仅措辞**。
- **(b) 必需插件首装失败 → 是否拦截启动**:现状**永远 exit 0**(`:436`),首装失败的 worker 会**缺插件白屏**起来。M4 建议:**首次安装(`isNew=true`)的插件下载/安装失败 → init 失败(非 0 退出)阻断启动**;版本更新失败则隔离继续(有 last-good)。
  - 落地草案:`processPlugin`/`main` 区分 `isNew` 失败与更新失败;`isNew` 失败累加 `criticalFailed`,主流程末尾 `if (criticalFailed > 0) process.exit(1)`。
  - **需你拍板**:是否引入「必需插件」白名单(只有白名单内首装失败才阻断),还是「任何首装失败都阻断」。建议先「任何首装失败都阻断」(简单),白名单后置。

---

## 5. P5-5 —— 迁移老节点(cnp + 部署)

把现有「worker 持 root-JWT 直连 admin 拉插件」迁到「worker → 本机 agent(tokenless)→ console(pull-token)」。

### 5.1 前置(console 侧,每个 ns 一次)

1. **建/确认 namespace**:`POST /api/namespaces {code}`(code = 该机 agentId),返回 **show-once agentKey**(写进该机 agent 的 `AGENT_KEY`)。
2. **签 pull-token**:`POST /api/namespaces/{id}/rotate-pull-token` → 明文(写进该机 agent 的 `PULL_TOKEN`,**只此一次**)。
3. **补发布链**:把该 ns 下各 service 现有插件在 console 台账登记并 publish(plugin → service → service-plugin → upload .tgz → releases/publish),使 `GET /api/distribution/plugins` 能返回与老 admin 等价的清单。**这是迁移工作量主体**(数据搬运)。

### 5.2 每节点步骤(逐机,可灰度)

对每台 worker 宿主:

1. **部署 agent**(一宿主一个):`.env` 配 `WS_URL=ws://<console>/ws/agent`、`AGENT_ID`/`AGENT_KEY`(5.1.1)、`PLATFORM_URL=http://<console>`、`PULL_TOKEN`(5.1.2)、`PLUGIN_NAMESPACE=<ns code>`、`PLUGIN_SERVE_HOST=0.0.0.0`、发布 18082(选项 A)。挂 `docker.sock` + `MANAGED_PROJECTS_ROOT`。
2. **改 worker 配置**:按 §2 重写该机各 worker 的 `sync-plugins.config.json`(`adminUrl`→本机 agent、去 `adminToken`、`apiPath:/plugins`、`service` 填各自 serviceCode)。
3. **滚动重启 worker**:走已有优雅链(`/api/k8s/shutdown` → restart;见 [[reference_graceful_shutdown_http_only]]),重启时 init 脚本即从本机 agent 同步。
4. **验证**:worker 起来后 `storage/plugins` 内插件齐、UI 不白屏;agent `/health` cache 有命中;console `fetch_record` 有该 service 记录。

### 5.3 灰度 / 回滚

- **灰度**:先迁 1 台非关键 worker(或 admin 兜底节点的备机),验证一轮再铺开。
- **回滚**:保留旧 `sync-plugins.config.json` 为 `.bak`;回滚=换回 .bak + 重启(旧 root-JWT 链路在迁移窗口内**先别停**,保证可回退)。老 admin 的 `t_service_hub:queryPlugin` 端点在所有节点迁完前不下线。

### 5.4 注意

- **root-JWT 退场时机**:所有节点迁完 + 观察期过,才回收老 adminToken / 下线 `t_service_hub:queryPlugin`。
- **agentKey/pull-token 是 show-once**:签发即写入对应 agent `.env`,丢了只能 rotate 重签。
- **service_code 对齐**:worker 配置里的 `service` 必须等于 console 台账 `serviceCode`,否则清单为空(INNER JOIN 无命中)。

---

## 6. 验收

- **P2**:真 worker(非 curl 模拟)重启后从本机 agent 装插件成功;断网 agent → 保留 last-good 不白屏;空清单不误清(M4)。
- **P5-5**:一台老节点平滑迁入,UI 正常、插件齐;可回滚到旧链路。

## 7. 风险 / 取舍

- §1 网络可达若选 A,18082 在内网宿主网卡可见——按 steering「内网不较真」接受;要严格 loopback 走 C。
- P5-5 的发布链数据搬运(5.1.3)是人工/脚本工作量大头,需逐 ns 逐 service 核对插件清单与老 admin 一致。
- P2-3「首装失败即阻断」会让缺包节点起不来(预期行为),但需确保 console 发布链先备齐,否则迁移期反而拦住启动——故 **P5-1 前置(发布链)必须先于 P2-3 阻断策略上线**。
