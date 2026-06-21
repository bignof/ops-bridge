# service-platform

service-platform 是机群插件分发与发布的**控制平台后端**(单 admin 人类会话)。它维护命名空间 / 服务 / 插件 / 版本 / 发布等台账,接收插件包 `.tgz` 上传与发布(单活 + 历史回滚),并向各部署节点提供基于 per-namespace pull token 的拉包分发接口。

> 本服务为纯 API 后端(FastAPI)。前端 SPA 与多阶段镜像构建留待 P1-SPA 计划补充。

## 能力

- 单 admin 登录签发 JWT(`POST /auth/login`,**不在 `/api/` 前缀下**;会话回显 `GET /auth/me`);`/api/**` 默认拒绝中间件守门(白名单:`/auth/login` / `/api/distribution/` / `/health`),逐路由 `Depends(require_session)` 作纵深防御
- 命名空间 / 服务 / 插件 / 服务-插件 等台账逐资源 CRUD(响应统一 camelCase + `{count, rows, page, pageSize, totalPage}` 分页信封)
- 命名空间 provision / rotate(经外部 service-hub),pull token / agent key 仅签发时返回一次(show-once)
- 插件包 `.tgz` 上传:`version` 取自包内 `package.json`(对齐 NocoBase `build --tar` 产物)
- 发布 / 历史激活 / 回滚:单活语义 + 事务保证(UNIQUE 约束 + IntegrityError 兜底)
- 节点拉包分发:`GET /api/distribution/plugins` 返回 `[{pluginName, version, url}]`(供 `sync-plugins.js` 直接解析),`GET /api/distribution/download/{id}` 归属式下载(防 IDOR),并写 `fetch_record` 拉取记录

## 本地起步

```bash
# 1. 安装依赖(建议虚拟环境)
pip install -r requirements.txt

# 2. 准备配置:复制示例并按需填写
cp .env.example .env
#   至少要设 PLATFORM_ADMIN_PASSWORD 与 PLATFORM_JWT_SECRET(后者须 ≥32 字符,否则启动被拒)

# 3. 启动(开发热重载)
uvicorn app.main:app --reload
```

启动时 `app/main.py` 的 lifespan 会自动执行 Alembic 迁移到最新 schema(见下「数据库迁移」),无需手动建表。缺省 `DATABASE_URL` 走本地 sqlite 文件,仅供本机起步;生产请指向独立 MySQL8 库 `service_platform`。

在线接口文档(`/docs` / `/redoc` / `/openapi.json`)**默认关闭**(生产安全:这些端点不在 `/api/` 前缀下,default-deny 中间件不拦,开着即匿名暴露全 API 面)。本机调试需要时设 `PLATFORM_ENABLE_DOCS=true` 再启动,关闭时三者均返回 404。

## 跑测试

测试地基用 service-hub 范式的 `client` fixture(临时文件库 + swap `app.main.database` + dispose),走真实 sqlite,不 mock DB。固定加 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 关闭 pytest 第三方插件自动加载,避免环境插件干扰:

```bash
pip install -r requirements-dev.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

> `pytest.ini` 已按消息文本精确过滤 Pydantic 2.12 + FastAPI 0.115 在请求体模型重建时产生的良性告警(详见该文件注释),非契约问题。

## 数据库迁移

服务启动时自动迁移到最新 schema。需要手动执行时:

```bash
alembic -c alembic.ini upgrade head
```

若库是旧版本通过自动建表初始化、但还没有 `alembic_version`,首次启动会自动补齐基线(stamp head),不重复建表;若检测到「部分初始化」的遗留 schema(有受管表但缺 `alembic_version` 且非全集),会拒绝启动要求人工介入。

## Docker

```bash
docker build -t service-platform:latest .
docker run -d --name service-platform \
  --env-file .env \
  -p 8080:8080 \
  -v service-platform-plugins:/app/data/plugins \
  service-platform:latest
```

镜像已 COPY `alembic.ini` 与 `migrations/`,启动即自迁移。`PLUGIN_STORAGE_DIR`(缺省 `./data/plugins`)是插件包落盘目录,**生产须挂持久卷或共享存储**(多节点拉包需可达),否则容器重建即丢包;Dockerfile 已对其声明 `VOLUME`。

## 环境变量

变量名与默认值以 `app/config.py` 为权威来源;改名前先改 config.py。完整示例见 `.env.example`。

| 变量                       | 说明                                                                 | 默认值                          |
| -------------------------- | -------------------------------------------------------------------- | ------------------------------- |
| `HOST`                     | 服务监听地址                                                         | `0.0.0.0`                       |
| `PORT`                     | 服务监听端口                                                         | `8080`                          |
| `DATABASE_URL`             | 台账库连接串,支持 SQLite / MySQL                                    | `sqlite:///./service-platform.db` |
| `PLATFORM_ADMIN_USER`      | admin 登录用户名                                                     | 空串                            |
| `PLATFORM_ADMIN_PASSWORD`  | 【生产必改】admin 登录密码,空串=无法登录                            | 空串                            |
| `PLATFORM_JWT_SECRET`      | 【生产必改】JWT 签名密钥,**须 ≥32 字符,否则启动即被拒绝**          | 空串                            |
| `PLATFORM_JWT_TTL`         | JWT 有效期(秒)                                                     | `28800`(8h)                   |
| `PLATFORM_ENABLE_DOCS`     | 在线接口文档(`/docs` `/redoc` `/openapi.json`)开关,默认关(生产安全) | `false`                         |
| `SERVICE_HUB_URL`          | 外部 service-hub 地址(命名空间 provision / rotate)                 | 空串                            |
| `HUB_ADMIN_TOKEN`          | 【生产必改】调用 service-hub 的管理令牌,仅服务端持有,绝不下发浏览器 | 空串                            |
| `PLUGIN_STORAGE_DIR`       | 插件包 `.tgz` 落盘目录(生产须挂卷 / 共享存储)                      | `./data/plugins`                |
| `PLUGIN_DOWNLOAD_BASE_URL` | 分发响应 `url` 前缀                                                  | 空串                            |
| `PLUGIN_MAX_UPLOAD_BYTES`  | 应用层单包上传字节上限(由 `app/routers/plugin_versions.py` 直接读,非 config.py) | `209715200`(200MB)           |

## 与 service-hub / 分发节点的关系

```
  admin(浏览器)
    │ 登录 + 台账维护 + 上传/发布
    ▼
  service-platform ──── provision / rotate 命名空间(HUB_ADMIN_TOKEN) ────▶ service-hub
    │                                                                        (签发 per-ns
    │ /api/distribution/plugins  (Bearer = per-ns pull token)                 pull token /
    │ /api/distribution/download/{id}                                         agent key)
    ▼
  各部署节点 / service-agent ── 用本命名空间 pull token 拉包(sync-plugins.js)
```

- **平台 → hub**:命名空间的 provision / rotate 由本平台经 `SERVICE_HUB_URL` + `HUB_ADMIN_TOKEN` 调 service-hub 完成;hub 在签发 / 轮换时一次性返回 pull token 与 agent key(show-once,平台不留明文)。
- **节点 → 平台**:各部署节点用其命名空间的 pull token(`Authorization: Bearer <plain>`)调本平台 `/api/distribution/*` 拉取本命名空间的已发布插件包;token 不属该命名空间一律拒绝,下载走 id 归属式校验防越权(IDOR)。
- service-hub 自身是面向 agent 的控制服务(WebSocket 接入 + 下发 compose 指令),与本平台是两个独立服务,详见 `../service-hub/README.md`。

## 上传大小上限

插件包上传受**两层**限制,缺一不可:

1. **应用层**:`PLUGIN_MAX_UPLOAD_BYTES`(缺省 200MB),端点先看 `Content-Length` 预判、再以实际字节数兜底;另对包内 `package.json` 有 1MB 解压炸弹守卫(`MAX_PKG_JSON_SIZE`)。
2. **nginx 边缘**:反向代理须同步放开 `client_max_body_size`(缺省仅 1MB),否则大包在 nginx 即被 `413` 拦下,根本到不了应用层。两处上限应协调一致。
