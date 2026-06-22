# hub + platform 合并为 service-console · 执行计划

> 决策(2026-06-22 用户拍板):**彻底合并(一个服务 + 一个 DB)**;落点**重命名 `service-console`**;DB **压成一套全新初始迁移**(弃两条 Alembic 链);**保留** agent `plugin_cache`(P1-1,agent 侧不受影响)。
> 背景:platform→hub 耦合本就只一个 `hub_client.py` 模块(httpx+admin-token);两服务各带独立 DB/迁移/测试;all-in-one 已单镜像。合并主要省「容器内 HTTP 跳 + 两套 codebase 心智」+ agent 单上游。

## 终态
- **service-console**:单 FastAPI 进程 = platform 路由 + SPA + hub 的 `/ws/agent` WS 端点 + hub 路由(logs/rolling/nodes)+ hub store(agent 连接/订阅)。
- **一个 DB + 一套 Alembic**(`0001_initial` 建全表)、**一份 config**。
- 删 `hub_client.py`,platform 路由**进程内直调** hub store/逻辑(去 httpx + 内部 admin-token)。
- all-in-one 镜像:**单进程 uvicorn + nginx 单上游**(去掉 hub/platform 双 program);CI `docker-publish` 出单 `service-console` 镜像。
- **agent 不动**(plugin_cache 保留;WS 控制 + 插件回源后续可收成单上游,非本次)。

## 步骤(每步过测试门,绿了才进下一步)

| 步 | 内容 | 测试门 |
| --- | --- | --- |
| **S1** | 落点重命名:`git mv service-platform service-console`;修目录名引用(CI/Dockerfile/compose/README) | service-console(原 platform)测试全绿 |
| **S2** | 并入 hub 代码:hub `app` 模块(store/routers/ws/models/config/api_support/force_guard)并入 console;console `main.py` 挂 `/ws/agent` + include hub 路由 + 启动初始化 hub store。先不删 hub 目录、不动 DB | console 启动 OK;hub 路由可访问;import 修通 |
| **S3** | 合 config:hub settings 并入 console 一份(SERVICE_HUB_URL/HUB_ADMIN_TOKEN 等内部项删) | config 测试绿 |
| **S4** | 合 DB + 迁移(**高风险**):合并 `db_models`(hub 表 + platform 表,先查表名无冲突);统一 `db.py`(一个 engine/Base);删两套 `migrations/`,alembic 重生成单一 `0001_initial` 建全表 | 测试库按新迁移建;console 全量绿 |
| **S5** | 进程内直调(**高风险**):platform 路由 `hub_client.*` → 直接调 hub store/逻辑;删 `hub_client.py` + 相关 config;改 `test_hub_client` 等 | console 全量绿 |
| **S6** | 合测试:hub tests 并入 console;修 import;原 platform+hub 用例(+调整)全绿 | 全量绿 |
| **S7** | 镜像/CI/nginx:all-in-one Dockerfile/supervisord 改单进程;nginx 单上游;`docker-publish.yml` 单 `service-console` 镜像、删 hub 镜像 job | 镜像构建通过;57 床冒烟 |
| **S8** | 清理:删 `service-hub` 旧目录;文档/README 指向 service-console;全量 + e2e(ws/logs/rolling) | 全量 + e2e 绿 |

## 风险与守门
- **S4/S5 最险**(DB 合并 + 解耦);各完成后跑 console 全量,再继续。
- WS 端点整合后必跑 hub 的 ws/logs/rolling e2e(`validate_logs_stream_e2e` / `validate_phase1_e2e`)。
- 表名冲突:S4 前先 grep 两边 `__tablename__` 比对,有冲突先改名。
- 旧目录到 S8 才删,前面保留以便对照/回退。
