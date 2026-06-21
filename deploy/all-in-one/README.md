# service-console 单镜像(hub + platform + nginx 三合一)

把 **service-hub**、**service-platform**、**nginx** 打进**一个镜像**,一个 `docker run` 起全套运维控制台,省去多容器 compose 的内网编排。适合内网运维控制台这种「一台机器一套」的简单部署。

> 需要独立伸缩 hub / platform、或多副本时,仍用 [`service-platform/deploy/`](../../service-platform/deploy/) 的三容器分离部署。本目录是「简单优先」的一体化方案。

## 架构

```
浏览器 / agent ──:80──▶ nginx(容器内唯一对外面)
                          ├─ /ws/agent/  → hub      127.0.0.1:8081  (agent WebSocket)
                          └─ 其余 (SPA + /api)→ platform 127.0.0.1:8080
        supervisord 托管 hub / platform / nginx 三进程,任一崩溃自动重启
        sqlite 库 + 插件包落 /data(挂宿主卷持久化)
```

- hub、platform 只监听容器内 `127.0.0.1`,**不对宿主机暴露**;唯一入口是 nginx 的 80。
- 两个服务各自在启动时自动跑 alembic 迁移(`hub` 经 `db.init_schema`,`platform` 经 lifespan),首次启动即建库。
- 容器级只配**一套** env;`run-hub.sh` / `run-platform.sh` 负责把同名变量(`PORT` / `DATABASE_URL`)按进程隔离。`ADMIN_TOKEN` 同时作为 platform 调 hub 的 `HUB_ADMIN_TOKEN`。

## 构建

构建上下文必须是 **monorepo 根**(镜像要同时拷 `service-hub/` 与 `service-platform/`):

```bash
# 在 services-monorepo 根目录执行
docker build -f deploy/all-in-one/Dockerfile -t service-console:latest .
```

## 运行

```bash
cd deploy/all-in-one
cp .env.example .env
#  填好 ADMIN_TOKEN / PLATFORM_ADMIN_PASSWORD / PLATFORM_JWT_SECRET(强随机)
docker compose up -d
```

或纯 `docker run`:

```bash
docker run -d --name service-console \
  -p 80:80 \
  -e ADMIN_TOKEN=<强随机> \
  -e PLATFORM_ADMIN_USER=admin \
  -e PLATFORM_ADMIN_PASSWORD=<强随机> \
  -e PLATFORM_JWT_SECRET=<强随机≥32> \
  -v $(pwd)/data:/data \
  service-console:latest
```

浏览器开 `http://<宿主IP>/` 即控制台 UI;远程 agent 的 `WS_URL` 指向 `ws://<宿主IP>/ws/agent`。

## 环境变量

| 变量 | 必填 | 缺省 | 说明 |
| --- | --- | --- | --- |
| `ADMIN_TOKEN` | ✅ | — | hub 管理 token;platform 调 hub 复用它 |
| `PLATFORM_ADMIN_USER` | ✅ | `admin` | 控制台登录用户名 |
| `PLATFORM_ADMIN_PASSWORD` | ✅ | — | 控制台登录密码 |
| `PLATFORM_JWT_SECRET` | ✅ | — | 会话 JWT 签名密钥(≥32 字符) |
| `HTTP_PORT` | | `80` | 宿主对外端口(compose 用) |
| `DATA_DIR` | | `./data` | 宿主持久化目录(compose 用) |
| `HUB_DATABASE_URL` | | `sqlite:////data/hub/service-hub.db` | hub 库;可切 MySQL |
| `PLATFORM_DATABASE_URL` | | `sqlite:////data/platform/service-platform.db` | platform 库;可切 MySQL |
| `SERVICE_HUB_URL` | | `http://127.0.0.1:8081` | platform→hub 内网地址(单镜像默认无需改) |
| `HUB_ADMIN_TOKEN` | | = `ADMIN_TOKEN` | platform 调 hub 的 token(默认复用) |
| `PLUGIN_STORAGE_DIR` | | `/data/plugins` | 插件包落盘目录 |
| hub 调优项 | | 见各服务 | `HEARTBEAT_TIMEOUT` / `ROLLING_*` 等直接透传 |

## 数据持久化

`/data` 下三块,务必挂宿主卷:`/data/hub`(hub sqlite)、`/data/platform`(platform sqlite)、`/data/plugins`(插件包)。
hub 的 sqlite 存 agent 凭据,删了要重新 provision agent。**多节点拉插件包**时,`/data/plugins` 需放共享存储/对象存储(单机部署忽略)。

## TLS

默认只起 80。启用 443:① 把证书挂到容器 `/etc/nginx/certs`(compose 取消 `./certs` 卷注释);② 在 `nginx.conf` 取消 443 server 段注释并填证书文件名;③ 重建/重启容器。

## 日志 / 运维

- `docker logs service-console` 看三进程合并日志(supervisord 已把 stdout/stderr 汇到容器输出)。
- `docker exec service-console supervisorctl status` 看 hub / platform / nginx 各自状态;`supervisorctl restart platform` 单独重启某进程。
