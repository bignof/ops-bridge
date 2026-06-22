# 零中断滚动重启 — 实现计划 3 / 3：cnp 平台（@orchisky/plugin-service-hub）

> **For agentic workers:** 本计划在 **cnp 仓库**（`packages/plugins/@orchisky/plugin-service-hub`）实现，是薄触发层。配套设计：`services-monorepo/docs/2026-06-18-zero-downtime-rolling-restart-design.md`（v2）。依赖计划 2 的 hub 端点 `POST /api/rolling-restart`、`GET /api/rolling-restart/{task_id}`。

**Goal:** 平台增加"一键无感重启"：选 namespace(=agentId) + serviceName → 调 hub rolling-restart → 轮询进度展示。

**Architecture:** feign(`serviceHubClient`) 加两个 ApiType 调 hub 新端点（自动带 `X-Admin-Token`）；新 RPC 资源 `serviceRolling` 两个 action（`rollingRestart`/`rollingStatus`，`loggedIn`）；client 加一个插件设置页（选 agent+service、触发、轮询进度）。逻辑全在 hub，平台只代理 + 展示。

**Tech Stack:** NocoBase 1.7（TS + React18 + antd5）、`@nocobase/actions`、`@nocobase/client`、`axios`（feign 内已用）。

## Global Constraints

- **测试**：该插件无测试基建，**不要在 cnp 跑 `yarn test` / `install -f`**（会清共享 dev 库，见团队约定）。本计划用 `yarn dev` 热重载 + 手动/联调验证；feign 纯函数可选加 1 个 vitest（见 Task 1 备注）。
- action 规范：参数走 `ctx.action.params`；错误用 `ctx.throw`；末尾 `await next()`；handler 放 `actions/<feature>/`，不内联进 plugin.ts（照本插件既有风格）。
- ACL 默认拒绝：新 action 必须 `acl.allow`。沿用本插件既有 `'loggedIn'` 粒度。
- feign 调用自动带 `X-Admin-Token`（`serviceHubClient.sendRequest` 已强制），故 hub 鉴权天然满足；`SERVICE_HUP_URL`/`ADMIN_TOKEN` 由部署在 NocoBase environment 变量配置（现状已配）。
- 提交中文（conventional 前缀英文）；提交前 `git branch --show-current` 确认分支；**勿擅自 push**。
- i18n：用户可见文案过 `useT()`；新 key 同步加 `zh-CN.json`/`en-US.json`。

## File Structure

- Modify `.../src/server/feign/serviceHubClient.ts` — `ApiType` 加 `ROLLING_RESTART=10`、`ROLLING_STATUS=11`；`getRequestInfo` 加两 case。
- Create `.../src/server/actions/rolling/index.ts` — 定义资源 `serviceRolling` + `registerActions` + ACL。
- Create `.../src/server/actions/rolling/restart.ts` — `rollingRestart` / `rollingStatus` handler。
- Modify `.../src/client/index.tsx` — 注册一个插件设置页（按钮 + 进度）。
- Create `.../src/client/RollingRestartPage.tsx` — 设置页组件。
- Modify `.../src/locale/{zh-CN,en-US}.json` — 文案。

---

### Task 1: feign 增加两个 ApiType

**Files:**
- Modify: `packages/plugins/@orchisky/plugin-service-hub/src/server/feign/serviceHubClient.ts`

- [ ] **Step 1: 改 `ApiType` 枚举**

在 `RETRY_COMMENT = 9,` 后追加：
```ts
  /** 触发滚动重启 */
  ROLLING_RESTART = 10,
  /** 查询滚动重启进度 */
  ROLLING_STATUS = 11,
```

- [ ] **Step 2: 改 `getRequestInfo`**

在 `case ApiType.RETRY_COMMENT:` 的 return 之后、`default:` 之前追加：
```ts
    case ApiType.ROLLING_RESTART:
      return {
        method: 'POST',
        path: '/api/rolling-restart',
      };
    case ApiType.ROLLING_STATUS:
      return {
        method: 'GET',
        path: '/api/rolling-restart/{task_id}',
      };
```

- [ ] **Step 3:（可选）feign 单元测试**

> 该插件无 vitest 基建；若要加，新建 `src/server/feign/__tests__/serviceHubClient.test.ts`：
```ts
import { describe, it, expect } from 'vitest';
import { ApiType, getRequestInfo } from '../serviceHubClient';

describe('getRequestInfo rolling', () => {
  it('maps rolling restart + status', () => {
    expect(getRequestInfo(ApiType.ROLLING_RESTART)).toEqual({ method: 'POST', path: '/api/rolling-restart' });
    expect(getRequestInfo(ApiType.ROLLING_STATUS)).toEqual({ method: 'GET', path: '/api/rolling-restart/{task_id}' });
  });
});
```
跑：`yarn test:server packages/plugins/@orchisky/plugin-service-hub/src/server/feign/__tests__/serviceHubClient.test.ts`（scoped，安全；内存运行不碰共享库）。若不加测试，跳过本步。

- [ ] **Step 4: 提交**

```bash
git add packages/plugins/@orchisky/plugin-service-hub/src/server/feign/serviceHubClient.ts
git commit -m "feat(service-hub): feign 增加 rolling-restart/status 两个 ApiType"
```

---

### Task 2: serviceRolling 资源 + 两个 action + ACL

**Files:**
- Create: `.../src/server/actions/rolling/restart.ts`
- Create: `.../src/server/actions/rolling/index.ts`

**Interfaces:**
- `serviceRolling:rollingRestart`（POST）入参 `{agentId, serviceName, force?}` → 透传 hub → `ctx.body = { taskId }`
- `serviceRolling:rollingStatus`（GET/POST）入参 `{taskId}` → 透传 hub → `ctx.body = task`

- [ ] **Step 1: 写 handler**

`src/server/actions/rolling/restart.ts`：
```ts
import { Context, Next } from '@nocobase/actions';
import { ApiType, sendRequest } from '../../feign/serviceHubClient';

export const rollingRestart = async (ctx: Context, next: Next) => {
  const { agentId, serviceName, force } = ctx.action.params.values || {};
  if (!agentId || !serviceName) {
    ctx.throw(400, 'agentId 与 serviceName 必填');
  }
  const data = await sendRequest(ctx, ApiType.ROLLING_RESTART, { agentId, serviceName, force: !!force });
  ctx.body = data;
  await next();
};

export const rollingStatus = async (ctx: Context, next: Next) => {
  const { taskId } = ctx.action.params.values || {};
  if (!taskId) {
    ctx.throw(400, 'taskId 必填');
  }
  // getRequestInfo 用 {task_id} 占位符做路径替换
  const data = await sendRequest(ctx, ApiType.ROLLING_STATUS, { task_id: taskId });
  ctx.body = data;
  await next();
};
```

- [ ] **Step 2: 写资源定义 + ACL**

`src/server/actions/rolling/index.ts`（照本插件其它 `actions/*/index.ts` 的 `registerActions(app)` 模式）：
```ts
import { rollingRestart, rollingStatus } from './restart';

export function registerActions(app: any) {
  app.resourceManager.define({
    name: 'serviceRolling',
    actions: {
      rollingRestart: { handler: rollingRestart, method: 'post' },
      rollingStatus: { handler: rollingStatus, method: 'post' },
    },
  });
  app.acl.allow('serviceRolling', ['rollingRestart', 'rollingStatus'], 'loggedIn');
}
```
> `autoRegisterActions(app, __dirname)`（plugin.ts 既有调用）会自动扫到 `actions/rolling/index.ts` 的 `registerActions`，无需改 plugin.ts。

- [ ] **Step 3: 手动验证（dev）**

启动/热重载 nocobase-hub（或本地 dev）。用已登录态调用（test-runner 或浏览器 devtools），先验**鉴权链路通**（hub 无在线 agent 时应回 failed/由 hub 处理，但平台层应 200 透传 taskId）：
```
POST /api/serviceRolling:rollingRestart   body: {"agentId":"cnp-test","serviceName":"memory-share"}
预期：200，body 含 taskId（后台编排由 hub 跑）
POST /api/serviceRolling:rollingStatus    body: {"taskId":"<上一步 taskId>"}
预期：200，body 含 status/nodes
```
若 403：检查登录态 + ACL allow；若 500「SERVICE_HUP_URL is empty」：检查 nocobase-hub 的 environment 变量 `SERVICE_HUP_URL`/`ADMIN_TOKEN`。

- [ ] **Step 4: 提交**

```bash
git add packages/plugins/@orchisky/plugin-service-hub/src/server/actions/rolling/
git commit -m "feat(service-hub): 增加 serviceRolling 资源(rollingRestart/rollingStatus)+ACL"
```

---

### Task 3: client 设置页（一键 + 进度）

**Files:**
- Create: `.../src/client/RollingRestartPage.tsx`
- Modify: `.../src/client/index.tsx`
- Modify: `.../src/locale/zh-CN.json`、`.../src/locale/en-US.json`

- [ ] **Step 1: 写设置页组件**

`src/client/RollingRestartPage.tsx`：
```tsx
import React, { useState } from 'react';
import { Card, Input, Button, Space, message, Descriptions, Tag } from 'antd';
import { useAPIClient } from '@nocobase/client';

export const RollingRestartPage = () => {
  const api = useAPIClient();
  const [agentId, setAgentId] = useState('');
  const [serviceName, setServiceName] = useState('');
  const [task, setTask] = useState<any>(null);
  const [running, setRunning] = useState(false);

  const poll = async (taskId: string) => {
    for (let i = 0; i < 120; i++) {
      const { data } = await api.resource('serviceRolling').rollingStatus({ values: { taskId } });
      setTask(data?.data ?? data);
      const status = (data?.data ?? data)?.status;
      if (['done', 'failed', 'interrupted', 'degraded'].includes(status)) return;
      await new Promise((r) => setTimeout(r, 3000));
    }
  };

  const start = async () => {
    if (!agentId || !serviceName) {
      message.warning('请填 agentId 与 serviceName');
      return;
    }
    setRunning(true);
    setTask(null);
    try {
      const { data } = await api.resource('serviceRolling').rollingRestart({ values: { agentId, serviceName } });
      const taskId = (data?.data ?? data)?.taskId;
      if (!taskId) throw new Error('未返回 taskId');
      message.success('已触发滚动重启');
      await poll(taskId);
    } catch (e: any) {
      message.error(e?.message || '触发失败');
      console.error(e);
    } finally {
      setRunning(false);
    }
  };

  const color = (s: string) =>
    ({ done: 'green', degraded: 'gold', failed: 'red', interrupted: 'red', running: 'blue' } as any)[s] || 'default';

  return (
    <Card title="无感滚动重启">
      <Space direction="vertical" style={{ width: '100%' }}>
        <Input addonBefore="agentId" value={agentId} onChange={(e) => setAgentId(e.target.value)} placeholder="namespaceCode" />
        <Input addonBefore="serviceName" value={serviceName} onChange={(e) => setServiceName(e.target.value)} placeholder="如 memory-share" />
        <Button type="primary" loading={running} onClick={start}>一键无感重启</Button>
        {task && (
          <Descriptions bordered size="small" column={1} style={{ marginTop: 16 }}>
            <Descriptions.Item label="状态"><Tag color={color(task.status)}>{task.status}</Tag></Descriptions.Item>
            <Descriptions.Item label="错误">{task.error || '-'}</Descriptions.Item>
            <Descriptions.Item label="节点进度">
              {(task.nodes || []).map((n: any) => (
                <div key={n.address}><Tag color={color(n.status)}>{n.status}</Tag>{n.address} {n.error || ''}</div>
              ))}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Space>
    </Card>
  );
};
```

- [ ] **Step 2: 注册设置页**

`src/client/index.tsx` 的 `load()` 内（替换原空壳/注释占位）：
```tsx
import { RollingRestartPage } from './RollingRestartPage';
// ... 在 Plugin.load() 中：
    this.app.pluginSettingsManager.add('service-hub-rolling', {
      title: '无感滚动重启',
      icon: 'ReloadOutlined',
      Component: RollingRestartPage,
      aclSnippet: 'pm.service-hub-rolling',
    });
```
> 用 1 段路径（`service-hub-rolling`），避免 NocoBase 三级嵌套设置页跳隔壁插件的已知坑。

- [ ] **Step 3: i18n**

`src/locale/zh-CN.json` 加（en-US 给英文）：
```json
{ "无感滚动重启": "无感滚动重启", "一键无感重启": "一键无感重启" }
```
（页面内中文已直写；如需严格 i18n 再过 `useT()`。最小可用先这样，键值同步两份文件。）

- [ ] **Step 4: 提交**

```bash
git add packages/plugins/@orchisky/plugin-service-hub/src/client/ packages/plugins/@orchisky/plugin-service-hub/src/locale/
git commit -m "feat(service-hub): client 增加无感滚动重启设置页(触发+进度轮询)"
```

---

### Task 4: 端到端联调验证（49 测试床）

> 前置：计划 1（agent 新镜像部到 49 的 cnp-dev-agent）+ 计划 2（hub 新镜像）已上 49；测试床 memory-share-1/2 + 网关在跑（spec §9）。

- [ ] **Step 1: 部署三方新版本到 49**（agent 镜像、hub 镜像、nocobase-hub 插件）。确认 `cnp-dev-agent` 配了 `NACOS_SERVER=192.168.0.30:8848`/`NACOS_NAMESPACE=dev` 等新 env；hub 配了 `ROLLING_*`（用默认即可）；nocobase-hub environment 有 `SERVICE_HUP_URL`/`ADMIN_TOKEN`。

- [ ] **Step 2: 起压测**（在 49 内网）：沿用 spec §9 / 之前的 `/tmp/loadgen.sh` 打网关 `http://192.168.0.30:18890/api/health`，记录 non-200。

- [ ] **Step 3: 平台点"一键无感重启"**：agentId=`cnp-test`（注：49 的 agent AGENT_ID 实际值以 `/data/memory-share` agent 配置为准，联调前确认）、serviceName=`memory-share`，点按钮。

- [ ] **Step 4: 验收**：
  - 进度页两节点依次 in-progress→done，最终 `done`；
  - 压测 **non-200 = 0**；
  - **回归（必过）**：再用平台原有"下发命令"功能做一次 `update`（带 token，经 hub→agent）确认仍可换镜像；裸调 hub `dispatch`（不带 token）应 403（`_require_admin_token`，未配置时 503；验 B1）。

- [ ] **Step 5: 记录**：把联调结果（请求数/non-200/耗时）记入 spec §9 或本计划末尾。

---

## Self-Review（已核对）

- **Spec 覆盖**：平台只传 `{agentId, serviceName}`（§3/§4.4）✅；feign 带 admin token（向后兼容 §4.5、B1）✅；一键 + 进度（§10 选 taskId+轮询）✅；端到端验收含零中断 + B1 鉴权 + update 回归（§9）✅。
- **类型/命名一致**：资源 `serviceRolling`、action `rollingRestart`/`rollingStatus`、ApiType `ROLLING_RESTART`/`ROLLING_STATUS`、hub 路径 `/api/rolling-restart`(+`/{task_id}`) 与计划 2 一致；client 调 `api.resource('serviceRolling').rollingRestart/rollingStatus`。
- **占位符**：无（client i18n 取最小可用，已注明）。
- **测试取舍**：平台无测试基建，逻辑正确性由计划 2（hub）单测覆盖；平台层用手动 + 端到端联调验证（避免 cnp `yarn test` 清库风险）。
