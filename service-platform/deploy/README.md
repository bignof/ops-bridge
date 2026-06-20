# service-platform 部署栈(自带 nginx 反代,hub/platform 均不暴露宿主机面)

本目录是「节点控制」特性的隔离部署栈:用一个 **compose 自带的 nginx** 统一对外(80/443),
**service-hub** 与 **service-platform** 都**不再向宿主机发布端口**。目的是满足节点控制的阻塞
验收门——「异网段直连 hub 被拒」:hub/platform 既然没有宿主机面,外部网段就没有 TCP 可达。

> 这是自带 nginx 的栈(不是复用现网 nginx)。栈内 nginx 是唯一对外面。

## 拓扑

```
                          ┌─────────────────────────────────────────────┐
                          │                  宿主机                      │
   运维浏览器             │   ┌───────────────┐                          │
   (SPA + /api) ──443/80──┼──▶│     nginx      │  唯一对外面(80/443)     │
                          │   │ (1.27-alpine) │                          │
   远程 agent             │   └──────┬────────┘                          │
   (/ws/agent/...) ─443/80┼──────────┤  [ docker 网络 svc_internal ]     │
                          │          │                                   │
                          │     ┌────┴───────────────┬───────────────┐   │
                          │     │ /ws/agent/ →        │ 其余一切 →      │   │
                          │     ▼                     ▼                │   │
                          │  ┌──────────────┐   ┌─────────────────┐    │   │
                          │  │ service-hub  │   │ service-platform │    │   │
                          │  │   :8080      │   │      :8080       │    │   │
                          │  │ (无 ports)   │   │   (无 ports)     │    │   │
                          │  └──────┬───────┘   └────────┬────────┘    │   │
                          │         │  ▲  platform 服务端 │             │   │
                          │         │  └─ SERVICE_HUB_URL ┘             │   │
                          │         │     (内网调 hub,不经 nginx)       │   │
                          └─────────┼───────────────────────────────────┘  │
                                    │                                       │
                            外部 MySQL8 / Redis(机群,经 host.docker.internal 出站)
```

要点:

- **nginx 是唯一对外面**。`service-hub` / `service-platform` 的 compose 服务都不写 `ports`,
  只接入内网 `svc_internal`,宿主机/异网段无法直连 → 满足「异网段直连 hub 被拒」。
- **浏览器只与 platform 通信,永不直连 hub**。platform 是 BFF:前端走 platform 的 `/`(SPA)
  与 `/api/*`;platform **服务端**再经 `SERVICE_HUB_URL=http://service-hub:8080`(走内网)调 hub。
- **nginx 只把 `/ws/agent/*` 路由到 hub**,其余一切(`/`、`/api/*`、`/assets/*` …)路由到 platform。
  hub 的 `/api/*` **不**经 nginx 暴露(它只在内网给 platform 用)。

### 路由契约(与代码核实一致)

| 入口路径          | nginx 上游               | 说明 |
| ----------------- | ------------------------ | ---- |
| `/ws/agent/...`   | `service-hub:8080`       | 远程 agent 的 WebSocket(hub `app/routers/agent_ws.py`: `@router.websocket("/ws/agent/{agent_id}")`),带 WS 升级头 + 长读超时 |
| `/`、`/api/*`、其余 | `service-platform:8080`  | platform 单端口同时出 SPA 与 API(`app/main.py`:StaticFiles 托管 SPA 于 `/`,各 `/api/...` router) |

## 运维须知:agent 的 WS_URL 必须改指向 nginx 边缘

隔离后 agent **不再能直连 hub 的宿主机端口**(hub 已无 `ports`)。原先 agent 的连接地址
(`service-agent/.env.example` 的 `WS_URL`)形如:

```
# 旧(直连 hub 宿主机端口,隔离后不可达):
WS_URL=ws://<hub-host>:8080/ws/agent
```

部署本栈后,**必须**把每个 agent 的 `WS_URL` 改为指向本栈 nginx 边缘:

```
# 新(经 nginx 边缘;证书就绪后用 wss):
WS_URL=ws://<edge-host>/ws/agent
# 或启用 TLS 后:
WS_URL=wss://<edge-domain>/ws/agent
```

(agent 会在 `WS_URL` 后自行追加 `/{AGENT_ID}` 与 `?key=...`,nginx 的 `/ws/agent/` 前缀
location 会原样转发到 hub。)

## 网络取舍:为何不用 `internal: true`

`svc_internal` 是**普通 bridge 网络**,**没有**设 `internal: true`。原因:

- 生产 `DATABASE_URL` 多指向**机群外部 MySQL8 独立库**(见 `.env.example`),hub/platform 需要
  **出站**连外部 MySQL / Redis。`internal: true` 会切断容器到外部网络的出站,直接弄坏部署。
- 「异网段直连被拒」这一验收门,**靠「不发布端口」本身就已满足**(外部无 TCP 可达 hub/platform),
  无需 `internal: true`。

仅当 hub/platform 全部使用容器内 sqlite、且无任何外部依赖时,才可考虑 `internal: true`;本栈默认
不满足该前提,故不启用。

## 文件清单

| 文件 | 职责 |
| ---- | ---- |
| `docker-compose.yml` | 三服务编排:nginx(唯一对外)、service-hub、service-platform(后两者无 `ports`);`svc_internal` 内网;沿用 hub 的 healthcheck/volume/restart/logging |
| `nginx/nginx.conf`   | 边缘反代:`/ws/agent/` → hub(WS 升级头 + 长读超时),其余 → platform;`client_max_body_size` 与插件上传上限对齐;443 段证书占位 |
| `.env.example`       | 栈级环境变量示例(镜像名、hub `ADMIN_TOKEN`、platform `HUB_ADMIN_TOKEN`/`PLATFORM_*`、分库 `DATABASE_URL`、`PLUGIN_STORAGE_DIR` 等),敏感值占位 |
| `README.md`          | 本文件 |

## 部署步骤

1. 复制 env 并填写(敏感值勿提交):

   ```bash
   cp .env.example .env
   # 编辑 .env:填镜像名、ADMIN_TOKEN(且令 HUB_ADMIN_TOKEN 与之一致)、
   #          PLATFORM_ADMIN_PASSWORD、PLATFORM_JWT_SECRET(≥32 字符)、两个分库 DATABASE_URL。
   ```

2. 校验编排(不实际启动):

   ```bash
   docker compose -f docker-compose.yml --env-file .env config -q
   ```

3. 启动:

   ```bash
   docker compose -f docker-compose.yml --env-file .env up -d
   ```

4. 把各 agent 的 `WS_URL` 改指向本栈 nginx 边缘(见上「运维须知」)。

## 生产 TLS 证书挂载

本任务**不签证书**,默认在 80 端口提供服务,nginx.conf 的 443 段整段注释占位。启用 HTTPS 时:

1. 在 `docker-compose.yml` 的 `nginx` 服务取消 `./nginx/certs:/etc/nginx/certs:ro` 卷挂载注释,
   把宿主机证书目录挂进容器 `/etc/nginx/certs`;
2. 在 `nginx/nginx.conf` 取消 443 `server` 段注释,把 `ssl_certificate` / `ssl_certificate_key`
   指向 `/etc/nginx/certs/` 下的实际证书文件;
3. (可选)把 80 端口的 `server` 改为 `return 301 https://$host$request_uri;` 强制跳转 HTTPS。

证书就绪后,记得把 agent 的 `WS_URL` 由 `ws://` 改为 `wss://`(见上「运维须知」)。
