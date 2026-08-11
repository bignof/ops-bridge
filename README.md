# Ops Bridge

`Ops Bridge` 是一套远程运维控制系统的执行侧仓库：

- `service-agent` 部署在目标服务器上，通过 WebSocket 常连控制面，接收指令并执行 Docker Compose 操作、按巡检清单周期上报服务状态
- 控制面（hub）由 NocoBase 插件 `@orchisky/plugin-hub` 承担，代码在 `nocobase-pro` 仓库的 `packages/plugins/@orchisky/plugin-hub`，不在本仓库

> 历史说明：本仓库早期包含一个独立的 `service-hub`（Python/FastAPI 控制面原型），hub 侧能力迁入 NocoBase 插件后已于 2026-08 移除，实现仍可在 Git 历史中找到。

## 仓库结构

```text
.
├─ .github/workflows/      # CI / 镜像发布
├─ docs/                   # 架构说明、一期存档、后续路线
└─ service-agent/          # 部署在目标主机的执行代理
```

## 快速开始

```bash
cd service-agent
pip install -r requirements.txt
python agent.py
```

### Docker 方式

```bash
docker build -t orchidea/service-agent:local ./service-agent
```

运行所需的 `WS_URL`（指向 plugin-hub 的 `/ws/agent/<code>` 端点）、`AGENT_ID`、`AGENT_KEY` 等环境变量见 [`service-agent/README.md`](service-agent/README.md)。

## 发布约定

镜像发布 workflow 需要以下 secrets：

- `REGISTRY_URL`
- `REGISTRY_USERNAME`
- `REGISTRY_PASSWORD`
- `SERVICE_AGENT_IMAGE_NAME`

## 文档导航

- [架构说明](docs/ARCHITECTURE.md)
- [一期范围整理（存档）](docs/PHASE1_BASELINE.md)
- [一期验收清单（存档）](docs/PHASE1_ACCEPTANCE.md)
- [后续路线规划](docs/ROADMAP.md)
- [Service Agent 操作说明](service-agent/README.md)
