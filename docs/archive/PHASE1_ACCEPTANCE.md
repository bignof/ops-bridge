# 一期验收清单

本文档用于给一期收尾提供统一口径，避免“功能看起来差不多”和“已经可以正式收口”之间出现理解偏差。

## 验收目标

一期的目标不是做复杂平台，而是确认下面这条链路已经稳定可用：

1. Hub 创建 Agent 并签发凭据
2. Agent 稳定接入并保持在线
3. Hub 能下发 `restart` / `update`
4. Agent 能回传 `ack` 和最终结果
5. Hub 能保存最新状态、事件流和命令记录
6. 两个服务都能独立通过测试和镜像构建

## 自动化验收

### 1. Service Agent 基础测试

在 [`service-agent/README.md`](../service-agent/README.md) 对应目录运行：

```bash
pytest --cov=agent --cov=config --cov=core --cov=services --cov-report=term-missing --cov-fail-under=97 -q
```

通过标准：

- 全部测试通过
- 覆盖率门槛不低于 97%

### 2. Service Hub 基础测试

在 [`service-hub/README.md`](../service-hub/README.md) 对应目录运行：

```bash
pytest -q
```

通过标准：

- 全部测试通过

### 3. 跨服务一期联调

在仓库根目录运行：

```bash
python service-hub/scripts/validate_phase1_e2e.py
```

该脚本已经接入 [`ci.yml`](../.github/workflows/ci.yml) 中的 `phase1-e2e` 任务。

该脚本会自动完成：

- 构建 `service-hub` 与 `service-agent` 镜像
- 启动一套临时 Hub / Agent / 目标 compose 环境
- 创建 Agent 并等待接入
- 执行一次 `restart`
- 执行一次 `update`
- 校验事件流、命令持久化、Agent 健康状态与 Hub 重启后的恢复情况
- 校验缺目录、缺 compose 文件、Docker 不可用这 3 类常见失败场景

通过标准：

- `restart` 成功
- `update` 成功
- 命令事件顺序包含 `created -> ack -> result`
- Hub 重启后命令记录仍可查询
- Agent 能重新连回 Hub
- 常见失败场景能稳定返回预期的失败状态和错误信息

### 4. MySQL 持久化联调

如果需要补充验证 MySQL 场景，在仓库根目录运行：

```bash
python service-hub/scripts/validate_mysql_e2e.py
```

通过标准：

- MySQL 中的 Agent 凭据、命令记录、事件流在 Hub 重启后仍然完整可读

## 发布前人工检查

- [`service-hub/.env.example`](../service-hub/.env.example) 和 [`service-agent/.env.example`](../service-agent/.env.example) 与当前代码保持一致
- [`README.md`](../README.md)、[`docs/PHASE1_BASELINE.md`](PHASE1_BASELINE.md) 和服务内 README 没有过期描述
- Docker 发布 secrets 已按 [`README.md`](../README.md) 配置完成

## 一期通过口径

满足下面 3 条时，可以认为一期可以收口：

- 两个服务基础测试全部通过
- 跨服务联调脚本通过
- 没有未处理的阻塞问题影响 `restart` / `update` 主链路
