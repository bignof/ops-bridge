# service-platform P1-SPA（前端控制台）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** 用 React+Vite+antd+ProComponents 建 `service-platform/web/` 单页控制台,**仿照 NocoBase admin 观感**,复刻现分发平台 7 页(命名空间/服务/插件/服务插件/插件上传/插件发布[含历史/回滚]/获取记录)+ 登录,全部走 P1a 后端 API。最终 `vite build` 产物由 P1a 的 FastAPI StaticFiles 托管(单容器)。

**Architecture:** 纯前端 SPA,无自己的后端;`Authorization: Bearer <JWT>` 调 P1a。路由 react-router;表格/表单用 ProTable/ProForm 贴近 NocoBase 表格(筛选/添加/刷新/齿轮 + 抽屉表单)。**字段/列基线 = Task 0 派生的冻结基线 `docs/ui-baseline-p1.md`**(由 `docs/uiSchemas.sql` 现 7 页 schema 转写 + 字段语义查 `docs/fields.sql` + 动作→端点绑定查 `plugin-service-hub/src/server/actions/*/index.ts`);各资源 API 响应形状以 P1a 契约为准。

**Tech Stack:** Vite、React 18、TypeScript、antd 5、@ant-design/pro-components、react-router-dom、axios;测试 Vitest + @testing-library/react;lint eslint + prettier。

## Global Constraints

> **本计划依赖 P1a 上述契约定稿后执行**:本计划列出的所有跨计划契约(camelCase 字段命名、台账列表回可读名、列表分页信封 `{count,rows,page,pageSize,totalPage}`、发布主表 `GET /api/releases` 语义、`?namespaceId=`/`?serviceId=` 级联过滤、`GET /api/fetch-records`)以 P1a 计划(`2026-06-19-service-platform-plan-p1a-backend.md`)为权威来源。**P1a 上述契约钉死前不要派发本计划的 subagent**,否则前端会靠猜、集成期返工。

- 依赖 **P1a 后端**的 API 契约:登录 `POST /auth/login`→`{token}`、`GET /auth/me`;台账 `/api/{plugins,namespaces,services,service-plugins}`(GET 列表信封 `{count, rows, page, pageSize, totalPage}` / POST 201 / PATCH / DELETE 204,401 无 token,409 唯一冲突);上传 `POST /api/plugin-versions/upload` + `GET /api/plugin-versions?pluginId=`(list 同信封 `{count,rows,...}`);发布 `POST /api/releases/{publish,reactivate,rollback}` + `GET /api/releases`;获取记录 `GET /api/fetch-records`;命名空间 `POST /api/namespaces/{id}/rotate-key`、`/rotate-pull-token`(返回 show-once 明文)。
- **请求/响应字段全量 camelCase**(与 P1a 钉死的契约一致):服务表单字段为 `defaultImage`/`nacosServiceName`(**去掉历史草稿里 `default_image`/`nacos_service_name` 等 snake 残留**);发布体 `serviceId`/`pluginId`/`pluginVersionId` 等均 camel;级联查询参数 `?namespaceId=`/`?serviceId=` camel。
- **台账列表列直接用后端回的可读名字段,不再客户端拼 id→名**:P1a 台账 list 端点会 LEFT JOIN 关联表回 `namespaceCode`/`serviceName`/`pluginCode`/`version` 等只读字段,列直接展示它们;**namespace 列用 `code`**(name 为空时回退 code 作稳定标签)。
- **列表统一服务端分页**:ProTable 走服务端分页,`request` 把 `current`/`pageSize` 映射为后端 `page`/`pageSize` 传入;读响应信封 `{count, rows, page, pageSize, totalPage}`(`total: count`、`data: rows`)。低基数配置表如确定不分页须在该页注明「ProTable 客户端分页」消歧。
- **发布页主表 = `GET /api/releases`(不传 filter,后端按 isActive=true 取每绑定当前激活行)**;历史抽屉 = `GET /api/releases?serviceId=&pluginId=`(取该 service+plugin 的全部 spv 历史)。**不要**自建 over-service_plugin 的聚合端点。
- **级联走服务端过滤**:服务表单按 `?namespaceId=` 拉服务;服务插件页/发布抽屉按 `?serviceId=` 等服务端参数过滤(**别纯客户端拉全量再猜**)——旧 NocoBase 关联选择自动发服务端 filter,新手写 ProForm 无此 magic,必须显式带过滤参数。
- **UI 仿 NocoBase admin**(用户定):左侧栏分「配置」(命名空间/服务/插件/服务插件)、「发布」(插件上传/插件发布/获取记录)两组;列表页 ProTable 带「筛选/添加/刷新」工具条;新增/编辑用 Drawer 表单;关联选择用级联(命名空间→服务→插件→版本)。**字段/列照 Task 0 派生的 `docs/ui-baseline-p1.md` 逐项对齐,不漏字段/动作。**
- token 存内存(React context)+ sessionStorage(刷新保活);401 自动跳登录;**不写死 `/api/` 之外的绝对 URL**,baseURL 同源(`import.meta.env.BASE_URL`)。
- 用户可见文案中文;show-once 的 key/pull token 用弹窗展示 + 复制按钮(**用 copy-to-clipboard 包**,HTTP+内网 IP 下 navigator.clipboard 不可用,见根 CLAUDE.md / [[clipboard-needs-copy-to-clipboard]])。
- 测试最小但有:每页至少 1 个 render/交互 smoke(mock api);`npm run lint` + `npm run build` 必须过(CI 门)。
- 提交中文 `feat(platform-web): ...`;分支 `feat/service-platform`;勿 push。

## File Structure

```
service-platform/web/
  package.json            # vite react ts antd pro-components react-router axios copy-to-clipboard + dev: vitest @testing-library/react jsdom eslint prettier
  vite.config.ts          # base: '/', build.outDir 指向被 FastAPI 托管的目录(见 T7)
  tsconfig.json  index.html  .eslintrc.cjs
  src/
    main.tsx              # Router + AntdApp + AuthProvider
    api/client.ts         # axios 实例(baseURL 同源, 注入 Bearer, 401→logout)
    api/resources.ts      # 各资源 CRUD 封装(list/get/create/update/remove) + 上传/发布/分发
    auth/AuthContext.tsx  # token 存取 + useAuth
    auth/LoginPage.tsx
    auth/RequireAuth.tsx  # 路由守卫
    layout/AppShell.tsx   # 左侧栏(配置/发布两组) + 顶栏 + Outlet
    components/CrudTable.tsx   # 复用:ProTable + 工具条 + Drawer 表单(列/字段由 props 配置)
    components/ShowOnceModal.tsx  # 一次性密钥/token 展示 + 复制
    pages/NamespacesPage.tsx
    pages/ServicesPage.tsx
    pages/PluginsPage.tsx
    pages/ServicePluginsPage.tsx
    pages/PluginUploadPage.tsx
    pages/ReleasesPage.tsx
    pages/FetchRecordsPage.tsx
    test/setup.ts
```

---

### Task 0: 前端冻结基线派生（M-7 首个交付物，最先做）

**Files:** Create `services-monorepo/docs/ui-baseline-p1.md`(不写代码,纯文档交付物)

**目的**:spec M-7 把「转每页字段/列/动作/校验清单作冻结基线 + 验收门=逐页对照无缺失」定为 P1 首个交付物。**此清单是 Task 1-6 的字段/列/动作来源**,不先产出,后续页面无从对齐、Task 7 验收门不可执行。

**关键约束(为什么不能只靠一个 dump)**:
- 列/字段/校验 → 转写自 `docs/uiSchemas.sql`(**注意大写 S**)现 7 页 schema;字段**语义/中文标签**查 `docs/fields.sql`(非 uiSchemas)。
- **动作→端点绑定不在 uiSchemas.sql 里**:6 个自定义 action(`pluginPublisher`/`updateVersion`/`pluginRollback`/`credentialsRotate`/`queryPlugin` 等)在该 dump 中 grep=0,绑定存在未导出的 customRequests 表。**每个动作按钮调用的 P1a `resource:action` 必须从两处推**:① 现插件 action 注册源码 `packages/plugins/@orchisky/plugin-service-hub/src/server/actions/*/index.ts`(列出每个资源 define 了哪些 action,如 `t_namespace:credentialsRotate`、`t_service_plugin_version:pluginPublisher/updateVersion`);② 本计划 Global Constraints 的 P1a 端点表(把旧 NocoBase action 映射到 P1a 新端点,如 `credentialsRotate`→`POST /api/namespaces/{id}/rotate-key`、`pluginPublisher`→`POST /api/releases/publish`)。

- [ ] **Step 1:** 逐页(命名空间/服务/插件/服务插件/插件上传/插件发布[含历史/回滚]/获取记录)从 `docs/uiSchemas.sql` 转写出**列、表单字段、校验规则**;字段中文标签/语义对照 `docs/fields.sql` 补全。字段名一律记为 camelCase(对齐 P1a 契约)。
- [ ] **Step 2:** 逐页列出**动作按钮**(添加/编辑/删除/上传/发布/历史版本/重新激活/回滚/轮换密钥/轮换 pull token 等),**每个动作标注其调用的 P1a `resource:action` 或 HTTP 端点**(来源见上「关键约束」②;不确定的标 TODO 并指明去 `actions/*/index.ts` 哪个文件核对)。
- [ ] **Step 3:** 逐页显式标注**哪些块/动作是 P2 故意不做**(命令下发、命令状态/重试、命令历史 `t_comment_record`、在线/心跳实时列、滚动「一键无感上线」),与页面占位写法一致,避免 subagent 误建 P2 才有的 hub-relay 按钮。
- [ ] **Step 4: commit** `docs(platform-web): 前端冻结基线 ui-baseline-p1(逐页 列/字段/动作→端点/校验)`

---

### Task 1: web/ 脚手架 + api client + 登录 + 路由守卫 + AppShell

**Files:** Create `web/`(package.json/vite.config.ts/tsconfig/index.html/.eslintrc)、`src/main.tsx`、`src/api/client.ts`、`src/auth/{AuthContext,LoginPage,RequireAuth}.tsx`、`src/layout/AppShell.tsx`、`src/test/setup.ts`、`src/auth/__tests__/login.test.tsx`

**Interfaces:**
- Produces:`api/client.ts` 默认导出 axios 实例(注入 `Authorization`);`useAuth()`→`{token, login(user,pw), logout, user}`;`<RequireAuth>` 守卫;路由表(`/login`、`/` 下挂各页)。

- [ ] **Step 1: 初始化 vite react-ts** + 装依赖(antd、@ant-design/pro-components、react-router-dom、axios、copy-to-clipboard;dev:vitest、@testing-library/react、jsdom、eslint、prettier)。vite.config 配 `test`(jsdom)+ `build.outDir`(T7 定)。**安装后提交 `package-lock.json`**(本仓首个 npm 工程,无既有锁文件先例;T7 Dockerfile 用 `npm ci` 强制 lockfile)——若决定不入库 lockfile,则 T7 Dockerfile 改 `npm ci || npm install` 兜底。
- [ ] **Step 2: api/client.ts**
```ts
import axios from "axios";
const client = axios.create({ baseURL: "/" });
client.interceptors.request.use((c) => {
  const t = sessionStorage.getItem("platform_token");
  if (t) c.headers.Authorization = `Bearer ${t}`;
  return c;
});
client.interceptors.response.use((r) => r, (e) => {
  if (e.response?.status === 401) { sessionStorage.removeItem("platform_token"); location.hash = "#/login"; }
  return Promise.reject(e);
});
export default client;
```
- [ ] **Step 3: AuthContext + LoginPage + RequireAuth**(login 调 `POST /auth/login`,存 token 到 sessionStorage + context;RequireAuth 无 token→Navigate /login)。
- [ ] **Step 4: AppShell**(antd Layout:左 Sider 两组菜单[配置:命名空间/服务/插件/服务插件;发布:插件上传/插件发布/获取记录],右 Outlet;顶栏含登出)。
- [ ] **Step 5: main.tsx**(createHashRouter:`/login`→LoginPage;`/`→RequireAuth+AppShell,children 暂只 namespaces 占位)。
- [ ] **Step 6: 失败测试 login.test.tsx**(render LoginPage,mock client.post 返回 token,填表单点登录→断言跳转/存 token)。
- [ ] **Step 7:** `npx vitest run` 绿;`npm run lint && npm run build` 过。
- [ ] **Step 8: commit** `feat(platform-web): 脚手架 + api client + 登录/守卫 + AppShell`

---

### Task 2: 复用 CrudTable + 命名空间页（CRUD + show-once 范例）

**Files:** Create `src/components/{CrudTable,ShowOnceModal}.tsx`、`src/pages/NamespacesPage.tsx`、`src/api/resources.ts`、`src/pages/__tests__/namespaces.test.tsx`;Modify main.tsx 挂路由

**Interfaces:**
- Produces:`CrudTable<T>(props: { resource, columns, formFields, ... })` —— ProTable 列表 + 工具条(筛选/添加/刷新) + Drawer 增改 + 删除,内部调 `resources.list/create/update/remove`。`ShowOnceModal({title, value, open, onClose})`。

- [ ] **Step 1: resources.ts**(泛型 CRUD:`list(resource, { page, pageSize, ...filter })`→后端信封 `{count, rows, page, pageSize, totalPage}`(全量 camelCase)、`create/update/remove`;+ `rotateKey(id)`、`rotatePullToken(id)`)。
- [ ] **Step 2: CrudTable.tsx**(ProTable:columns 由 props;toolBar「添加」开 Drawer ProForm;行操作「编辑/删除」;**服务端分页**——ProTable `request` 把 `current`/`pageSize` 映射成后端 `page`/`pageSize`,读 `{ data: rows, total: count, success: true }`;创建/编辑成功 reload;**409→message.error "编码已存在"**)。
- [ ] **Step 3: ShowOnceModal.tsx**(展示明文 + 「复制」按钮用 `copy-to-clipboard`,按返回值提示成功/失败;关闭即不可再得)。
- [ ] **Step 4: NamespacesPage.tsx**(CrudTable:列=`code`/`name`[+在线/最近心跳列**占位 P2,显 "-"**];添加表单字段=`code`(必填)/`name`;**创建成功若响应含 `agentKey`→ShowOnceModal 展示**;行操作加「轮换密钥」「轮换 pull token」→调对应 API→ShowOnceModal 展示返回明文)。字段/列对照 Task 0 基线的命名空间页确保不漏。
- [ ] **Step 5: 失败测试 namespaces.test.tsx**(mock resources:render→点添加→填 code→提交→断言调用 create;点「轮换 pull token」→断言弹出 ShowOnceModal 含返回明文)。
- [ ] **Step 6:** vitest 绿 + lint + build 过。 **Step 7: commit** `feat(platform-web): CrudTable/ShowOnceModal + 命名空间页`

---

### Task 3: 服务 / 插件 / 服务插件 页（按字段表）

**Files:** Create `src/pages/{Services,Plugins,ServicePlugins}Page.tsx` + 各 smoke 测试;Modify main.tsx 挂路由

**说明**:三页复用 `CrudTable`,差异见字段表(非"similar to")。逐页对照 Task 0 的 `docs/ui-baseline-p1.md` 现页字段/列补全。**列直接用后端回的可读名字段(`serviceName`/`namespaceCode`/`pluginCode`),不客户端拼 id→名**;字段名全 camelCase。

| 页 | resource | 列 | 表单字段 | 特例 |
| --- | --- | --- | --- | --- |
| 服务 | services | namespaceCode(命名空间)/serviceCode/name(别名)/dir(目录)/nacosServiceName | namespaceId(选命名空间)、serviceCode(必填)、name、dir、defaultImage、nacosServiceName | namespaceId 用关联选择(`list('namespaces')` 拉选项);唯一冲突 409 提示 |
| 插件 | plugins | code/name(别名) | code(必填)、name | 唯一冲突 409 |
| 服务插件 | service-plugins | namespaceCode/serviceName/pluginCode | 级联:命名空间→服务→插件(三级联动选择) | 仅 列表/添加/删除(无编辑);唯一冲突 409 |

- [ ] **Step 1-3:** 逐页实现 + 各 1 个 smoke 测试(mock resources,断言列表渲染 + 添加调用)。**服务插件页重点测三级级联走服务端过滤**:选命名空间后调 `list('services', { namespaceId })` 拉服务(传 `?namespaceId=` 参数),选服务后调 `list('plugins')`;断言带了正确的过滤参数而非客户端全量拉取后 filter。
- [ ] **Step 4:** vitest + lint + build 过。 **Step 5: commit** `feat(platform-web): 服务/插件/服务插件 页`

> **P1 不做(P2 占位)**:本三页只做 列表/增删改;**不做**命令下发、命令状态/重试、命令历史(`t_comment_record`)等 P2 hub-relay 动作 —— 与命名空间页「在线/心跳列占位 P2」写法一致,Task 0 基线已显式标注这些为 P2,实现时勿误建相关按钮。

---

### Task 4: 插件上传页

**Files:** Create `src/pages/PluginUploadPage.tsx`、扩 `resources.ts`(`uploadPluginVersion(file)`、`listPluginVersions(params)`)、smoke 测试

- [ ] **Step 1: resources.uploadPluginVersion**(`FormData` 传 file 到 `POST /api/plugin-versions/upload`)。
- [ ] **Step 2: PluginUploadPage**(antd Upload 拖拽/选 .tgz → 上传 → 成功 message 显示解析出的 `{version}`;下方 ProTable 列出 plugin-version(列=`pluginCode`/`version`/`filename`,用后端回的可读名;`GET /api/plugin-versions?pluginId=` 服务端分页,信封 `{count,rows,...}`);**上传失败 400[未匹配插件]/409[版本已存在] → 明确错误提示**)。对照 Task 0 基线上传页。
- [ ] **Step 3: smoke 测试**(mock upload 成功/409,断言提示)。
- [ ] **Step 4:** 绿 + build。 **Step 5: commit** `feat(platform-web): 插件上传页`

---

### Task 5: 插件发布页（发布 + 历史版本 + 回滚）

**Files:** Create `src/pages/ReleasesPage.tsx`、扩 `resources.ts`(`publish/reactivate/rollback/listReleases`)、smoke 测试

- [ ] **Step 1: resources**(publish{serviceId,pluginId,pluginVersionId}、reactivate{spvId}、rollback{spvId};`listReleases()` 主表=`GET /api/releases` **不传 filter**(后端按 isActive=true 回每绑定当前激活行);`listReleaseHistory({serviceId,pluginId})`=`GET /api/releases?serviceId=&pluginId=`(取该 service+plugin 全部 spv 历史))。
- [ ] **Step 2: ReleasesPage**(ProTable 主表走 `listReleases()`,列=`namespaceCode`/`serviceName`/`pluginCode`/当前 `version`,**直接用后端回的可读名**,服务端分页;工具条「发布」开 Drawer:级联选 命名空间→服务→插件→版本(服务/插件级联按 `?namespaceId=`/`?serviceId=` 服务端过滤,见 Global Constraints)→ publish;行操作「历史版本」(打开抽屉调 `listReleaseHistory({serviceId,pluginId})` 列该 service+plugin 的 spv 历史,每行可「重新激活」reactivate)、「回滚」rollback;终态/active 用 Tag 标色)。对照 Task 0 基线插件发布页(历史版本/回滚动作)。
- [ ] **Step 3: smoke 测试**(mock:点发布→断言 publish 调用且未带 filter 拉主表;历史抽屉点回滚→断言历史按 serviceId+pluginId 过滤、rollback 调用)。
- [ ] **Step 4:** 绿 + build。 **Step 5: commit** `feat(platform-web): 插件发布页(发布/历史/回滚)`

---

### Task 6: 获取记录页

**Files:** Create `src/pages/FetchRecordsPage.tsx`、smoke 测试

- [ ] **Step 1:** 只读 ProTable(列=`namespaceCode`/`serviceName`/`pluginCode`/`version`/`fetchDate`,用后端回的可读名),**服务端分页**(`GET /api/fetch-records` 传 `page`/`pageSize`,信封 `{count,rows,...}`)+ 筛选。此审计表行数无界,**必须服务端分页,勿全量返回**。对照 Task 0 基线获取记录页。
- [ ] **Step 2:** smoke(mock list,断言渲染 + 服务端分页参数)。 **Step 3: commit** `feat(platform-web): 获取记录页`

---

### Task 7: 构建集成（FastAPI 托管 SPA + 多阶段镜像 + CI）

**Files:** Modify `service-platform/Dockerfile`(P1a 单阶段→多阶段)、`app/main.py`(StaticFiles 托管)、`web/vite.config.ts`(outDir)、新增 `.github/workflows` 前端 job(若仓库用 GH Actions;否则文档化)

- [ ] **Step 1: vite build outDir** 指向 `service-platform/app/static`(或独立 `web/dist` 由 Dockerfile 拷贝)。
- [ ] **Step 2: app/main.py 挂 StaticFiles**
```python
import os
from fastapi.staticfiles import StaticFiles
_static = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="spa")  # 放在 include_router 之后, 兜底前端路由
```
> 注:StaticFiles 挂 `/` 必须在所有 `/api`、`/auth`、`/health` router **之后** include,避免吞掉 API。SPA hash 路由(createHashRouter)天然不与后端 path 冲突。
- [ ] **Step 2b: 最小 CSP + 安全响应头中间件**(XSS 缓解 —— **无论 token 存哪都要补**;纯内存持有也挡不住注入脚本复用已登录的 api client。本控制台是对全机群有 dispatch=RCE 能力的运维 admin 会话)。在 `app/main.py` 加一个 HTTP 中间件给所有响应注入:`Content-Security-Policy: default-src 'self'`(**禁内联脚本**,不放 `unsafe-inline`/`unsafe-eval`;antd/Vite 产物为外链 JS/CSS,inline 样式如需放开仅加 `style-src 'self' 'unsafe-inline'`)、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`。
```python
@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", "default-src 'self'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp
```
> 备选:若部署确定由前置 nginx 统一下发 CSP/安全头,则在 README 注明「CSP 由 nginx 边缘下发」并省去本中间件 —— 但二者必居其一,不能两边都不做。
- [ ] **Step 3: 多阶段 Dockerfile**
```dockerfile
FROM node:20-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build           # 产物 → /web/dist

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY --from=web /web/dist ./app/static
EXPOSE 8080
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
```
- [ ] **Step 4: CI**(若 `.github/workflows/ci.yml` 存在:加 service-platform-web job = `npm ci && npm run lint && npm run build`;并加 service-platform-backend job = pytest。否则在 README 文档化命令)。
- [ ] **Step 5: 手动验收**:`docker build` service-platform → 起容器 → 浏览器开 `/` 登录 → 各页能列表/增删改、上传、发布/回滚(对 P1a 后端)。
- [ ] **Step 5b: 逐页对照基线打勾(M-7 验收门)**:对照 Task 0 的 `docs/ui-baseline-p1.md`,逐页核对实现的 列/表单字段/动作按钮/校验**无缺失**(尤其每个动作确实调到了基线标注的 P1a `resource:action`/端点);差异逐条记录并补齐或显式标 P2。**此为 P1 前端验收门,未打勾不算完成。**
- [ ] **Step 5c: 安全头验收**:`curl -I` 控制台任一路径,断言**响应头含 `Content-Security-Policy`**(及 `X-Content-Type-Options`/`X-Frame-Options`);若选 nginx 下发方案,则在部署环境核到边缘已注入。
- [ ] **Step 6: commit** `feat(platform-web): FastAPI 托管 SPA + 多阶段镜像 + CI`

---

## Self-Review

- **执行前置(M-7 + 跨计划契约)**:Task 0 先产出冻结基线 `docs/ui-baseline-p1.md`(逐页 列/字段/动作→P1a `resource:action`/校验;动作绑定从 `plugin-service-hub/src/server/actions/*/index.ts` + 端点表推,**不靠 uiSchemas.sql**),T7 Step 5b 逐页对照打勾作验收门 ✅。本计划依赖 P1a 契约定稿后执行(已在 Global Constraints 顶部标注)。
- **页面覆盖**(对照现 7 页):命名空间(T2)、服务/插件/服务插件(T3)、插件上传(T4)、插件发布+历史+回滚(T5)、获取记录(T6)+ 登录/壳(T1)✅。
- **UI 仿 NocoBase**:ProTable 工具条 + Drawer 表单 + 左侧配置/发布两组 + 级联选择,字段对照 Task 0 基线 ✅。
- **跨计划契约对齐 P1a**:① 字段全量 camelCase(`defaultImage`/`nacosServiceName`,去 snake 残留)；② 台账列直接用后端可读名(`namespaceCode`/`serviceName`/`pluginCode`/`version`),不客户端拼 id→名,namespace 列用 code；③ 列表信封 `{count,rows,page,pageSize,totalPage}` + ProTable 服务端分页；④ 发布主表 `GET /api/releases` 不传 filter,历史抽屉传 serviceId+pluginId；⑤ 级联走 `?namespaceId=`/`?serviceId=` 服务端过滤 ✅。
- **安全**:Bearer 注入 + 401→登录;show-once 弹窗 + copy-to-clipboard(内网 HTTP 坑);**T7 加最小 CSP(`default-src 'self'`,禁内联脚本)+ 安全响应头 + 「响应头含 CSP」验收**(XSS 真缓解,与 token 存储选型正交;备选 nginx 边缘下发,二者必居其一)✅。
- **占位扫描 / P2 边界**:CRUD 用 CrudTable + 字段表,非 "similar to";关键代码(client/StaticFiles/CSP/Dockerfile)给全;T3 显式列出 P1 不做的 P2 动作(命令下发、命令状态/重试、命令历史 `t_comment_record`),与命名空间页占位写法一致。
- **工程门**:每页 smoke(mock api)+ lint/build 门 + 手动验收;**T1 提交 `package-lock.json`**(或 Dockerfile `npm ci || npm install` 兜底)。
- **文档**:schema 引用统一为 `docs/uiSchemas.sql`(大写 S)+ 字段语义查 `docs/fields.sql`,已修正旧 `uischemas.sql` 拼写。
- **遗留执行期定**:① 在线/心跳列 P2 占位"-"(本期不接 hub);② StaticFiles outDir 与 Dockerfile 拷贝路径二选一,T7 已给两法择一;③ 「发布并一键无感上线」按钮属 P2(滚动),本期发布页不含;④ Task 0 中少数动作→端点绑定若 uiSchemas/源码均存疑,留 TODO 指向具体 `actions/*` 文件,执行 Task 0 时核定。

**遗留顾虑(非本计划可独立解决)**:本计划假定 P1a 已落地以下契约(camelCase 模型 + alias、台账 list 的 LEFT JOIN 可读名、分页信封/参数、`GET /api/fetch-records` 读端点、`GET /api/releases` filter 语义);这些是 P1a 待改项,**若 P1a 未同步则本计划契约悬空**——故强约束「P1a 契约定稿后再派本计划」。CSP 与 hub 网络隔离是不同层防护,hub 隔离(spec M-3)属横切部署任务,不在本前端计划。
