# 架构说明

## 目标

这套系统解决的是“平台侧统一下发运维动作，内网主机安全执行并回传状态”的问题。控制面与执行面分仓维护、运行时解耦。

## 组件划分

### 控制面：NocoBase 插件 `@orchisky/plugin-hub`（外部仓库）

代码在 `nocobase-pro` 仓库的 `packages/plugins/@orchisky/plugin-hub`。

- 提供 agent 的 WebSocket 接入点（`/ws/agent/<code>`）、鉴权与在线注册表
- 管理 namespace / service / agent / deployment 台账与命令下发、状态流转
- agent 连接建立时及 deployment 增删改（事务提交后）向 agent 推送覆盖式 `watch_targets` 巡检清单
- 接收 `status_report` 上报，落库 reported* 字段，驱动离线/劣化告警

### 执行面：`service-agent`（本仓库）

- 部署在目标服务器
- 通过 WebSocket 常连 plugin-hub
- 按目录粒度串行执行 `update` / `restart`（支持 graceful）
- 维护 hub 推送的 watch_targets 清单，独立巡检线程周期采集 `docker compose ps` 真实状态并上报

## 交互关系

```text
平台 / 第三方系统
        │ HTTP API
        ▼
NocoBase（@orchisky/plugin-hub）
        │ WebSocket（命令下发 / watch_targets 推送 / status_report 上报）
        ▼
service-agent
        │ docker compose
        ▼
目标主机业务容器
```

## 协议要点

- `watch_targets` 为覆盖式全量清单（`{deploymentId, dir}`），agent 侧整份替换、不做增量合并
- agent 离线时错过的清单推送不补偿，依赖重连时 hub 的连接建立全量推送兜底
- 命令结果走 ack/result + result_ack 的至少一次投递，agent 侧 outbox 补投

## 历史

本仓库曾以 monorepo 形式同时维护 `service-hub`（Python/FastAPI 控制面原型）与 `service-agent`；hub 侧能力迁入 NocoBase 插件后，`service-hub` 已于 2026-08 移除，实现见 Git 历史。`docs/PHASE1_*` 为该时期的存档文档。
