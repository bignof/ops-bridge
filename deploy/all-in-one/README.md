# service-console 单镜像(console + nginx 二合一)

把 **service-console**(hub + platform 已**进程内合并**为单一 FastAPI)与 **nginx** 打进**一个镜像**,一个 `docker run` 起全套运维控制台。适合内网运维控制台「一台机器一套」的简单部署。

> hub 与 platform 已不再是两个服务/两套库——见 `docs/plugin-distribution-redesign.zh-CN.md` 合并决策。

## 架构

```
浏览器 / agent ──:80──▶ nginx(容器内唯一对外面)
                          ├─ /ws/agent/       → console 127.0.0.1:8080  (agent WebSocket)
                          └─ 其余 (SPA + /api)→ console 127.0.0.1:8080
        supervisord 托管 console / nginx 两进程,任一崩溃自动重启
        单一 sqlite 库 + 插件包落 /data(挂宿主卷持久化)
```

- console 只监听容器内 `127.0.0.1:8080`,**不对宿主机暴露**;唯一入口是 nginx 的 80。
- 启动时自动跑 alembic 迁移(lifespan `database.init_schema`,单一 0001 建全 12 表),首次启动即建库;并恢复中断的滚动任务(`interrupt_running_rolling`)。
- 容器级配**一套** env;`run-console.sh` 设 `PORT=8080` / `DATABASE_URL`。`ADMIN_TOKEN` 是 hub 控制链路由(`/api/agents` 等)的 admin token(进程内合并后**无 platform→hub HTTP 跳**)。

## 构建

构建上下文是 **monorepo 根**(镜像拷 `service-console/`):

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
| `ADMIN_TOKEN` | ✅ | — | hub 控制链路由(`/api/agents` 等)的 admin token(进程内,无 platform→hub 跳) |
| `PLATFORM_ADMIN_USER` | ✅ | `admin` | 控制台登录用户名 |
| `PLATFORM_ADMIN_PASSWORD` | ✅ | — | 控制台登录密码 |
| `PLATFORM_JWT_SECRET` | ✅ | — | 会话 JWT 签名密钥(≥32 字符) |
| `HTTP_PORT` | | `80` | 宿主对外端口(compose 用) |
| `DATA_DIR` | | `./data` | 宿主持久化目录(compose 用) |
| `DATABASE_URL` | | `sqlite:////data/console/service-console.db` | 单一库;可切 MySQL |
| `PLUGIN_STORAGE_DIR` | | `/data/plugins` | 插件包落盘目录 |
| 调优项 | | 见 `app/config.py` | `HEARTBEAT_TIMEOUT` / `ROLLING_*` 等直接透传 console 进程 |

## 数据持久化

`/data` 下两块,务必挂宿主卷:`/data/console`(单一 sqlite 库,存 agent 凭据等)、`/data/plugins`(插件包)。
删库要重新 provision agent。**多节点拉插件包**时,`/data/plugins` 需放共享存储/对象存储(单机部署忽略)。

## TLS

默认只起 80。启用 443:① 把证书挂到容器 `/etc/nginx/certs`(compose 取消 `./certs` 卷注释);② 在 `nginx.conf` 取消 443 server 段注释并填证书文件名;③ 重建/重启容器。

## 日志 / 运维

- `docker logs service-console` 看 console + nginx 合并日志(supervisord 已把 stdout/stderr 汇到容器输出)。
- `docker exec service-console supervisorctl status` 看 console / nginx 状态;`supervisorctl restart console` 单独重启 console。
