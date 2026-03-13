# Ops Bridge

`Ops Bridge` 是一套成对交付的远程运维控制系统：

- `service-hub` 负责平台侧接入、Agent 管理、命令下发和状态持久化
- `service-agent` 部署在目标服务器上，负责接收指令并执行 Docker Compose 操作

这两个项目现在已经合并到同一个 monorepo 中，便于统一维护文档、CI、发布规范和阶段规划，同时保留原有 Git 历史。

## README 放置策略

- 根目录 `README.md` 只负责项目总览、协作方式和文档导航
- `service-agent/README.md` 保留 Agent 的部署与协议细节
- `service-hub/README.md` 保留 Hub 的运行、接口和对接说明

这样做的目的是把“仓库入口说明”和“服务操作手册”分开，避免一个 README 同时承担总览和细节，后期越来越难维护。

## 仓库结构

```text
.
├─ .github/workflows/      # Monorepo 统一 CI / 发布入口
├─ docs/                   # 架构说明、一期边界、后续路线
├─ service-agent/          # 部署在目标主机的执行代理
└─ service-hub/            # 平台侧控制服务
```

## 快速开始

### 运行 service-hub

```bash
cd service-hub
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 运行 service-agent

```bash
cd service-agent
pip install -r requirements.txt
python agent.py
```

### Docker 方式

两个服务都保留了各自的 `Dockerfile` 和 `docker-compose.yml`，可以按服务独立构建和部署：

```bash
docker build -t orchidea/service-hub:local ./service-hub
docker build -t orchidea/service-agent:local ./service-agent
```

## 协作约定

- 根目录统一维护 `.gitignore`、`.gitattributes`、`.editorconfig`
- 子目录只保留服务特有规则，避免重复配置
- GitHub Actions 统一从仓库根目录触发，子项目内部不再保留独立 workflow 入口
- 公共规划、阶段边界和架构决策尽量写在根目录 `docs/`

## 发布约定

根目录已经补了统一的 Docker 发布 workflow。接到 GitHub 新仓库后，建议预先配置这些 secrets：

- `REGISTRY_URL`
- `REGISTRY_USERNAME`
- `REGISTRY_PASSWORD`
- `SERVICE_AGENT_IMAGE_NAME`
- `SERVICE_HUB_IMAGE_NAME`

## 文档导航

- [架构说明](docs/ARCHITECTURE.md)
- [一期范围整理](docs/PHASE1_BASELINE.md)
- [一期验收清单](docs/PHASE1_ACCEPTANCE.md)
- [后续路线规划](docs/ROADMAP.md)
- [Service Hub 操作说明](service-hub/README.md)
- [Service Hub API 文档](service-hub/docs/THIRD_PARTY_API.md)
- [Service Agent 操作说明](service-agent/README.md)

## 当前建议

当前这套仓库已经适合进入“一个仓库、两套服务、统一规范”的维护方式。下一步如果要继续降维护成本，最值得做的是：

1. 抽一层共享协议模型，减少 Agent / Hub 对消息结构的重复维护。
2. 把发布流程里的镜像命名和环境变量约定固化下来。
3. 把命令失败结果整理得更清楚，减少排障时依赖字符串搜索日志。
