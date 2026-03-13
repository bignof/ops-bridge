# 架构说明

## 目标

这套系统解决的是“平台侧统一下发运维动作，内网主机安全执行并回传状态”的问题。当前仓库将控制面和执行面合并管理，但运行时仍保持解耦。

## 组件划分

### `service-hub`

- 对外提供 HTTP API 和 WebSocket 接入点
- 管理 Agent 注册、鉴权、在线状态和命令历史
- 维护命令状态流转：`queued`、`processing`、`success`、`failed`
- 负责持久化、审计和未来的平台扩展能力

### `service-agent`

- 部署在目标服务器
- 通过 WebSocket 常连到 `service-hub`
- 按目录粒度串行执行 `update` / `restart`
- 基于宿主机 Docker Compose 完成服务切换

## 交互关系

```text
平台 / 第三方系统
        │ HTTP API
        ▼
service-hub
        │ WebSocket
        ▼
service-agent
        │ docker compose
        ▼
目标主机业务容器
```

## Monorepo 组织原则

- 根目录承载共享规则、公共文档、统一 CI
- 服务内部保留各自运行入口、测试、Docker 构建和细化文档
- 只有真正稳定且被两边共同依赖的内容，才考虑上提为共享模块

## 当前不急于抽公共包的原因

虽然 Hub 和 Agent 共享一部分消息语义，但目前还没有形成足够稳定的协议层。现阶段更适合先统一文档、测试入口和命名规范，等消息模型稳定后，再考虑引入类似 `packages/shared-contracts` 的共享目录。

## 后续建议的共享层

等二期开始后，可以考虑补一个轻量共享层，优先提炼：

- WebSocket 消息模型
- 命令状态枚举和错误码
- 鉴权 / agent 注册相关的数据结构
- 联调脚本和集成测试夹具
