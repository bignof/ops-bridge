# Ops Bridge

`Ops Bridge` 是一套远程运维 + 插件分发控制系统:

- `service-console` —— 平台侧单一控制面(**原 service-hub + service-platform 已合并为一个 FastAPI 进程**):Agent 接入(WebSocket)、命令下发 / 滚动投放、状态持久化、插件分发回源、控制台 SPA。
- `service-agent` —— 部署在目标服务器上,接收指令执行 Docker Compose 操作,并(规划中)兼做每主机插件缓存与拓扑自动发现。

> hub + platform 的合并见 `docs/plugin-distribution-redesign.zh-CN.md` 决策与 `docs/plugin-distribution-dev-plan.zh-CN.md` 的 M 阶段(S1–S8)。保留原有 Git 历史。

## README 放置策略

- 根目录 `README.md` 只负责项目总览、协作方式和文档导航
- `service-agent/README.md` 保留 Agent 的部署与协议细节
- `service-console/README.md` 保留 console 的运行、接口和对接说明
- `deploy/all-in-one/README.md` 保留单镜像(console + nginx)部署说明

## 仓库结构

```text
.
├─ .github/workflows/      # Monorepo 统一 CI / 发布入口
├─ docs/                   # 当前设计 / 开发计划 / 评审(历史归 docs/archive/)
├─ deploy/all-in-one/      # service-console 单镜像(console + nginx)部署
├─ service-agent/          # 部署在目标主机的执行代理
└─ service-console/        # 平台侧单一控制面(hub + platform 合并)
```

## 快速开始

### 运行 service-console

```bash
cd service-console
pip install -r requirements.txt
# 必配 PLATFORM_JWT_SECRET(≥32)、PLATFORM_ADMIN_PASSWORD、ADMIN_TOKEN
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

前端 SPA 在 `service-console/web/`(`npm ci && npm run build` 产物落 `app/static`,由 console 同源托管)。

### 运行 service-agent

```bash
cd service-agent
pip install -r requirements.txt
python agent.py
```

### Docker 方式(推荐:单镜像 all-in-one)

```bash
# console + nginx 一体化单镜像(构建上下文 = 仓库根)
docker build -f deploy/all-in-one/Dockerfile -t orchidea/service-console:local .
docker build -t orchidea/service-agent:local ./service-agent
```

一键起 console 见 `deploy/all-in-one/README.md`。

## 协作约定

- 根目录统一维护 `.gitignore`、`.gitattributes`、`.editorconfig`
- 子目录只保留服务特有规则,避免重复配置
- GitHub Actions 统一从仓库根目录触发
- 公共规划、阶段边界和架构决策写在根目录 `docs/`

## 发布约定

根目录统一的 Docker 发布 workflow(`.github/workflows/docker-publish.yml`)出两个镜像:
`orchidea/service-console`(all-in-one)与 service-agent。建议配置 secrets:

- `REGISTRY_URL`
- `REGISTRY_USERNAME`
- `REGISTRY_PASSWORD`
- `SERVICE_AGENT_IMAGE_NAME`

## 文档导航

- [插件分发改造 · 总开发计划](docs/plugin-distribution-dev-plan.zh-CN.md) —— 当前主计划(P0 + 合并 M + P1–P5)
- [插件分发改造 · 设计](docs/plugin-distribution-redesign.zh-CN.md) / [.html](docs/plugin-distribution-redesign.zh-CN.html)
- [ultracode 评审(2026-06-22)](docs/review-ultracode-2026-06-22.zh-CN.md)
- [Service Console 操作说明](service-console/README.md)
- [Service Agent 操作说明](service-agent/README.md)
- 历史文档(node-control / 一期 / 早期评审等)见 [`docs/archive/`](docs/archive/README.md)
