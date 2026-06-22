# service-platform P1b（存量数据/包迁移）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`)。

**Goal:** 一次性迁移脚本:把现 NocoBase 分发平台库 `nocobase_hub@192.168.0.30:3306` 的台账(7 表)+ 插件包 .tgz 字节,导入新 `service-platform` 的 MySQL 库 `service_platform` + 本地卷。**含两处关键正确性**:① `plugin_version.version` **解包 .tgz 读 package.json.version 重导**(旧值是文件名 split 的垃圾值,直搬会让全机群切流量后每次重下);② 单活规整 + 唯一约束去重。验收门 = **同节点连续两次 sync 第二次全跳过、0 下载**。

**Architecture:** `service-platform/migrate/` 下纯函数(映射/清洗/版本重导,可单测)+ 编排脚本(读旧库→映射清洗→按依赖序写新库,**保留原 id** 保 fk 一致;dry-run + 汇总)。复用 P1a 的 `app.db`/`app.db_models`/`app.storage`。包字节按旧 file-manager 后端(本地卷 vs S3-pro)分两路取。

**Tech Stack:** Python、SQLAlchemy(读旧库用裸 Core/反射 + 写新库用 P1a 模型)、PyMySQL、boto3(仅 S3 路径)、pytest。

## Global Constraints

- 依赖 **P1a 后端落地**(模型/迁移/storage 已建)。新库 schema = P1a 的 `service_platform`。
- 旧库真实列见已入库 `docs/fields.sql`/`collections.sql`(权威);旧业务**数据行**在迁移运行时从 `nocobase_hub@49` 实读(`OLD_DATABASE_URL`)。**只读旧库,绝不写旧库;勿碰 58 客户库。**
- **迁 7 表**:t_namespace→namespace、t_service→service、t_plugin→plugin、t_plugin_version→plugin_version、t_plugin_attachments→plugin_attachment、t_service_plugin→service_plugin、t_service_plugin_version→service_plugin_version。**不迁**:t_comment_record(命令实时读 hub)、t_print_version_update(打印助手,out of scope)。t_fetch_records 审计**可选迁**(默认不迁)。
- **额外读 `storages` 表(评审 M-6,非业务表不迁入,仅供取包定位)**:`t_plugin_attachments.storage belongsTo storages`(foreignKey `storageId`),决定每条附件字节落在哪个后端(local vs s3/s3-pro)、本地盘根在哪。**真实判据是 `storages.type`,不是 url 形状**;旧 local 行的 `url` 列运行时算出、DB 里常 NULL,光看 url 猜后端对 local 行不可靠。本地盘绝对路径 = `storages.options.documentRoot(缺省 storage/uploads) + storages.path + attachment.path + attachment.filename`(见 `@nocobase/plugin-file-manager` 的 `local.ts`:`getDocumentRoot`+`storage.path`+`record.path`+`record.filename`)。`old_reader` 须连同 storages 表一并读(或 JOIN),否则 detect_backend / 本地拼路径 / S3 剥 baseUrl 都做不准。
- **映射一律字段白名单(评审 M-8,关键)**:每个 `map_*` 必须**逐字段显式赋值新模型列**,**禁止 `{**old}` / `dict(old, ...)` 整包透传**。旧库反射会带回新模型没有的列——`agentKey`、物化的 `createdById`/`updatedById`(7 表 options 全 createdBy/updatedBy)、尤其 **`t_service_plugin` 上拼写错的 `namesapceId`**(见 `docs/fields.sql`,是旧库 typo,**不是**要保留的 `namespaceId`,别误当列搬运)。这些键透传到 `Model(**kwargs)` 会直接 `TypeError`,而最小 fixture 不含这些脏列时单测仍全绿、`--apply` 首行才崩。映射须只输出白名单内的列。
- **清洗规则**(评审 High-2/M-1/M-5/M3):
  - namespace:`agentKey` **丢弃**(新平台 show-once,不存);`pull_token_hash`=NULL(上线后运维轮换)。
  - service:`dir`=旧 dir、`default_image`=旧 image、`nacos_service_name`=NULL(新增,运维填);旧 `action` 冗余字段丢。
  - plugin_version:`version`=**重导**(解包 .tgz 读 package.json.version);旧 `url` 丢(改 storage_path);旧 denorm 的 namespaceId/serviceId 丢。
  - service_plugin_version:`previousVersionId` **丢弃**(死字段);`is_active`=`(旧 isActive=='yes')`、`is_rolled_back`=`(旧 isRolledBack=='yes')`;`spv_active_key`=`f"{service_id}-{plugin_id}"` 当 active 否则 NULL。
  - 唯一约束去重:`(plugin_id, version)` 重导后若撞(同插件多行映射到同 package.json 版本)→保留 versionOrder/createdAt 最新一行,**把指向被弃 plugin_version 的 spv.plugin_version_id 重映射到保留行**;`(service_id,plugin_id)` 单活规整(多 'yes' 时留 versionOrder 最大者 active,其余 inactive)。
  - **dedup 后 attachment 也要用同一 idmap 重映射(评审 M-7)**:`t_plugin_attachments` 各自带 `pluginVersionId`(hasOne),新模型 `PluginAttachment` 只有 index、**无 FK 兜底**;若只 remap spv 而不管 attachment,被弃 plugin_version 上的附件仍带旧 `pluginVersionId` 写入→**悬空附件(静默死数据)**。须用 `dedup_versions` 返回的同一 `idmap` 喂 `remap_attachment_version_ids`(或写 attachment 前按 idmap 改写),保证每个保留 plugin_version 恰对应一条 attachment、无悬空。
- **保留原 id 插入**(新库空):显式写 PK,fk 天然对齐;迁后重置 auto_increment。
- **dry-run 先行**:`--dry-run` 只读+打印将写的统计与冲突,不落库;确认后 `--apply`。
- 提交中文 `feat(platform-migrate): ...`;分支 `feat/service-platform`;勿 push。
- 测试(cwd=service-platform):`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest migrate/tests -q`(纯函数 + fixture,**不连真库**)。

## File Structure

```
service-platform/migrate/
  __init__.py
  config.py            # OLD_DATABASE_URL, SOURCE_PKG_MODE(local|s3), SOURCE_PKG_LOCAL_ROOT / S3_*(bucket/endpoint/ak/sk)
  old_reader.py        # 连旧库, 反射读 7 业务表 + storages 表行(返回 list[dict])
  mapping.py           # 纯函数(逐字段白名单, 禁 {**old}): map_namespace/service/plugin/plugin_version/attachment/service_plugin/spv (old dict -> new kwargs)
  pkg_source.py        # 取旧 .tgz 字节: local(读盘, 路径=documentRoot+storage.path+attachment.path+filename) / s3(boto3); detect_backend(按 storages.type)
  reimport.py          # reimport_version(tgz_bytes)->str(=package.json.version, 复用 app.storage.parse_tgz)
  cleanse.py           # dedup_versions(...) + remap_spv_version_ids(...) + remap_attachment_version_ids(...) + enforce_single_active(...)
  run.py               # 编排: 读->映射->取包+重导->清洗->按序写新库(保留id)->汇总; --dry-run/--apply
  tests/
    fixtures.py        # 构造旧行 dict + 内存 .tgz
    test_mapping.py  test_reimport.py  test_cleanse.py  test_run_dryrun.py
```

---

### Task 1: 旧库读取 + config

**Files:** Create `migrate/__init__.py`、`migrate/config.py`、`migrate/old_reader.py`、`migrate/tests/__init__.py`

**Interfaces:**
- Produces:`config`(env:OLD_DATABASE_URL、SOURCE_PKG_MODE、SOURCE_PKG_LOCAL_ROOT、S3_*);`old_reader.read_table(engine, table)->list[dict]`(SQLAlchemy 反射读全表行)、`read_all(engine)->dict[str,list[dict]]`(读 7 业务表 **+ `storages`**)。

- [ ] **Step 1: config.py**(env;SOURCE_PKG_MODE 默认 "local")。
- [ ] **Step 2: old_reader.py**(`create_engine(OLD_DATABASE_URL)` + `MetaData(reflect)` 读表为 dict 列表;表名常量:旧 7 表名 **+ `storages`**)。**`read_all` 须额外读 `storages` 表(评审 M-6)**——取包定位要靠 `t_plugin_attachments.storageId → storages.{type,options.documentRoot,path}` 才能判后端并拼本地盘绝对路径,光读 7 业务表拿不到。可单独返回 `storages` list,或在读 attachment 后按 storageId 关联进每行。
- [ ] **Step 3: 失败测试 test_old_reader 用内存 sqlite**(造一张临时表写两行→read_table 返回两 dict;**另造一张 `storages` 表→断言 `read_all` 结果含 storages 行**)。
- [ ] **Step 4:** 绿。 **Step 5: commit** `feat(platform-migrate): 旧库反射读取(含 storages)+ config`

---

### Task 2: 纯映射函数(old → new kwargs) + 清洗规则

**Files:** Create `migrate/mapping.py`、`migrate/tests/fixtures.py`、`migrate/tests/test_mapping.py`

**Interfaces:**
- Produces:`map_namespace(old)->dict`(剔 agentKey,pull_token_hash=None)、`map_service(old)`(dir/image→default_image,action 丢,nacos=None)、`map_plugin(old)`、`map_plugin_version(old, reimported_version)`(url 丢、version=参数)、`map_attachment(old, storage_path)`、`map_service_plugin(old)`、`map_spv(old)`(previousVersionId 丢,isActive/isRolledBack 字符串→bool,spv_active_key 计算)。均保留 `id`。
- **实现约束(评审 M-8):每个 `map_*` 逐字段显式赋值、只输出新模型白名单列,禁 `{**old}` 透传**。旧库脏列(`agentKey`、物化 `createdById`/`updatedById`、`t_service_plugin` 的 typo `namesapceId`)若漏到输出,下游 `Model(**kwargs)` 会 `TypeError`。`map_service_plugin` 尤其注意:旧库该表有拼写错的外键 **`namesapceId`**,而新 `ServicePlugin` 模型只有 `id/service_id/plugin_id/created_at`(无任何命名空间列,见 P1a 模型)——故 `namesapceId` **直接丢弃**;它是「看着像列、其实别带」的典型陷阱,别误当成要保留的 `namespaceId` 搬运。

- [ ] **Step 1: 失败测试 test_mapping.py**(对每个 map_* 给旧 dict→断言新 dict 字段正确)
```python
from migrate import mapping
def test_map_namespace_drops_agentkey():
    out = mapping.map_namespace({"id":1,"namespaceCode":"cnp-test","name":"希恩碧测试","agentKey":"SECRET",
                                 "createdAt":"2026-02-27 10:34:18","updatedAt":"..."})
    assert out["id"]==1 and out["code"]=="cnp-test" and out["name"]=="希恩碧测试"
    assert "agentKey" not in out and out["pull_token_hash"] is None

def test_map_spv_active_key_and_bool():
    out = mapping.map_spv({"id":5,"servicePluginId":3,"serviceId":1,"pluginId":2,"pluginVersionId":10,
                           "versionOrder":2,"isActive":"yes","isRolledBack":"no","previousVersionId":9,
                           "publishTime":"...","createdAt":"...","updatedAt":"..."})
    assert out["is_active"] is True and out["is_rolled_back"] is False
    assert out["spv_active_key"]=="1-2" and "previousVersionId" not in out

def test_map_spv_inactive_key_none():
    out = mapping.map_spv({...,"isActive":"no",...})
    assert out["is_active"] is False and out["spv_active_key"] is None

def test_map_service_image_and_drop_action():
    out = mapping.map_service({"id":1,"namespaceId":4,"serviceCode":"cnp-test-admin","name":"x",
                               "dir":"/data/admin","image":"img:1","action":"restart",...})
    assert out["dir"]=="/data/admin" and out["default_image"]=="img:1" and out["nacos_service_name"] is None
    assert "action" not in out

# 评审 M-8 反向断言: 每个 map_* 输出 keys ⊆ 新模型列, 且脏列被拦
import sqlalchemy as sa
from app import db_models
def test_map_service_plugin_drops_dirty_cols_incl_typo():
    # 旧库真实带脏列: typo namesapceId(fields.sql)、物化 createdById、agentKey
    old = {"id":3,"serviceId":1,"pluginId":2,"namesapceId":4,"createdById":7,"agentKey":"X",
           "createdAt":"2026-02-27 13:03:46","updatedAt":"..."}
    out = mapping.map_service_plugin(old)
    cols = set(sa.inspect(db_models.ServicePlugin).columns.keys())
    assert set(out) <= cols                                  # 无脏列漏出
    assert "namesapceId" not in out and "createdById" not in out and "agentKey" not in out
    # (对每个 map_* 都补一条同形 keys ⊆ Model.columns 断言)
```
- [ ] **Step 2: mapping.py + fixtures.py** 实现到绿(datetime 字符串按 ISO 解析;version 由调用方传入,见 Task3)。**每个 `map_*` 逐字段白名单赋值,禁 `{**old}`**;fixtures 的旧行 dict 须**带上脏列**(`namesapceId`/`createdById`/`agentKey` 等)以让上面反向断言真正有证伪力——否则最小 fixture 假绿。
- [ ] **Step 3: commit** `feat(platform-migrate): old→new 映射 + 清洗(剔 agentKey/previousVersionId/action, isActive→bool, spv_active_key)`

---

### Task 3: 版本重导 + 包字节取源(local/S3)

**Files:** Create `migrate/reimport.py`、`migrate/pkg_source.py`、`migrate/tests/test_reimport.py`

**Interfaces:**
- Produces:`reimport.reimport_version(tgz_bytes)->str`(=`app.storage.parse_tgz(bytes)["version"]`);`pkg_source.detect_backend(storage_row)->str`("local"|"s3",**按 `storages.type` 判**,见评审 M-6);`pkg_source.fetch_bytes(old_attachment_row, storage_row)->bytes`(按 backend 取旧 .tgz 字节;**两个入参都要**——附件给 path/filename/url,storage 给 type/documentRoot/path)。

- [ ] **Step 1: 失败测试 test_reimport.py**(用 fixtures 的内存 .tgz[package.json version=1.7.0-rc.2026...]→reimport_version 返回该 version;对比旧文件名 split 值证明二者不同)
```python
from migrate import reimport
from migrate.tests.fixtures import make_tgz
def test_reimport_reads_package_json_not_filename():
    # make_tgz 必须用真实 build --tar 布局: package.json 在 tar 根, 不加 package/ 前缀
    data = make_tgz("@business/plugin-x", "1.7.0-rc.20260618164415")
    assert reimport.reimport_version(data) == "1.7.0-rc.20260618164415"
    # 旧文件名 split('-').pop() 会得 "rc.20260618164415"(错), 证明必须重导
```
> **B1(P1b 侧):`make_tgz` fixture 至少含一个真实根级布局**(package.json 在 tar 根,**非 `package/` 前缀**)。实测真实 `build --tar` 包(`storage/tar/@business/plugin-mom-print-*.tgz`)首条目即根级 `package.json`(无 `package/`),与节点侧 `sync-plugins.js:236-238`(npm pack→`package/` 子目录,其它打包→根)同源。若 fixture 只造 `package/package.json` 而 P1a `parse_tgz` 只认该前缀,会**单测全绿但真实数据 100% BadPackage**。本插件 reimport 是 P1a `parse_tgz` 的 thin wrapper,故 `parse_tgz` 的根级回退须在 P1a 修(B1 主体);P1b 这边的责任是 fixture 用根级布局 + smoke 跑真实 .tgz(见 Task 6 Step 1)。
- [ ] **Step 2: reimport.py**(thin:调 `app.storage.parse_tgz`)。
- [ ] **Step 3: pkg_source.py**(评审 M-5/M-6,**从 Self-Review 遗留①升级为本步显式实现项**)
  - `detect_backend(storage_row)`:**按 `storages.type` 判**——`local`→"local",`s3`/`s3-pro`→"s3"(`SOURCE_PKG_MODE` 仅作 storages 缺失时的兜底覆盖)。**不要用 url 形状猜**:local 行的 `url` 列运行时算出、DB 里常 NULL,url 启发式对 local 行不可靠。
  - `fetch_bytes(old_attachment_row, storage_row)`:
    - **local**:绝对路径 = `os.path.join(documentRoot, storage_row['path'] or '', row['path'] or '', row['filename'])`,其中 `documentRoot = storage_row.options.get('documentRoot') or SOURCE_PKG_LOCAL_ROOT`(缺省 `storage/uploads`)。**必须拼上 `row['filename']`**——`t_plugin_attachments` 的 `path`(目录)与 `filename`(.tgz basename)是两列,只拼 path 会读到目录(`IsADirectoryError`/空字节),砸掉 P1b 核心目标(对齐 `@nocobase/plugin-file-manager` 的 `local.ts`:`documentRoot+storage.path+record.path+record.filename`)。
    - **s3**:用 attachment 的绝对 `url` 直取(boto3 get_object;**勿与 baseUrl 双拼**)。
  - 测试:**local 真实路径用例**——按 `<documentRoot>/<storage.path>/<attachment.path>/<filename>` 摆一个 tmp .tgz,构造对应 attachment_row+storage_row(`type='local'`)→`fetch_bytes` 读回字节相等;断言「漏 filename 只拼 path」会指到目录(反例守住 M-5);`detect_backend` 对 `type='local'`/`'s3-pro'` 分别返回 "local"/"s3"。s3 取字节路径用 monkeypatch 桩。
- [ ] **Step 4:** 绿。 **Step 5: commit** `feat(platform-migrate): 版本重导(package.json)+ 包字节取源(按 storages.type 判后端 + local 含 filename)`

---

### Task 4: 去重 + 单活规整 + spv 重映射

**Files:** Create `migrate/cleanse.py`、`migrate/tests/test_cleanse.py`

**Interfaces:**
- Produces:`cleanse.dedup_versions(versions:list[dict])->tuple[list[dict], dict[int,int]]`(按 `(plugin_id, version)` 去重,保留 versionOrder/createdAt 最新;返回[保留行, 旧版本id→保留版本id 重映射表]);`cleanse.remap_spv_version_ids(spvs, idmap)`;**`cleanse.remap_attachment_version_ids(attachments, idmap)`(评审 M-7,新增)**——按同一 idmap 改写 `plugin_attachment.plugin_version_id`,并对被弃 pv 的多余附件去重,保证每个保留 pv 恰对应一条附件;`cleanse.enforce_single_active(spvs)`(每 `(service_id,plugin_id)` 多 is_active 时留 version_order 最大者,其余 is_active=False+spv_active_key=None)。

- [ ] **Step 1: 失败测试 test_cleanse.py**
```python
# dedup: 两行 plugin_id=2 version="1.0" → 保留1行, idmap{旧弃id:保留id}
# remap(spv): spv.plugin_version_id 指向被弃 → 改为保留 id
# remap(attachment, 评审 M-7): 两条 attachment 各指向撞版的两个 pv → 用同一 idmap 重映射后,
#   保留 pv 上 attachment 不悬空(plugin_version_id 都在保留集内)且恰一条; 被弃 pv 无残留悬空附件
# single_active: (1,2) 有两行 is_active=True → 只留 version_order 最大者 active, 另一个 key=None
```
- [ ] **Step 2: cleanse.py** 到绿。
- [ ] **Step 3: commit** `feat(platform-migrate): 版本去重 + spv 重映射 + 单活规整`

---

### Task 5: 编排脚本(读→映射→重导→清洗→按序写, dry-run/apply)

**Files:** Create `migrate/run.py`、`migrate/tests/test_run_dryrun.py`

**流程(写新库依赖序,保留 id,单事务/分批)**:namespace → service → plugin → plugin_version(重导 version) → plugin_attachment(取包字节→`app.storage.store_tgz`→storage_path) → service_plugin → service_plugin_version(清洗后)。

- [ ] **Step 1: run.py**
  - 读旧库(old_reader.read_all,**含 storages 表**)。
  - 对每个旧 attachment:按 `storageId` 取对应 storage_row→`fetch_bytes(attachment_row, storage_row)` 取字节→`reimport_version`→得 version;`store_tgz(plugin_id, version_id, filename, bytes)`→storage_path(apply 模式才真落盘)。
  - map_* 生成新 kwargs(**逐字段白名单,禁 `{**old}`,见 M-8**);`dedup_versions`→拿 idmap→`remap_spv_version_ids(spvs, idmap)` **+ `remap_attachment_version_ids(attachments, idmap)`(评审 M-7,别漏)**→`enforce_single_active`。
  - `--dry-run`:打印每表将写行数 + 去重/单活冲突 + version 重导前后差异样例,**不落库不落盘**。
  - `--apply`:用 P1a `database.session_factory()` 按序 `session.add` 保留 id 写入;每表后 flush;末尾 commit;MySQL 重置 auto_increment。
  - 汇总:各表迁入数、去重数、单活规整数、version 被改写数、包字节迁移数/失败数。
- [ ] **Step 2: 失败测试 test_run_dryrun.py**(注入内存旧库[sqlite 造 7 旧表 + 1 个 fixture .tgz 源]→run(--dry-run)→断言汇总数字 + 不写新库;再 run(--apply, 新库内存 sqlite)→断言新库行数/version 已重导/单活唯一)。
- [ ] **Step 3:** 绿。 **Step 4: commit** `feat(platform-migrate): 迁移编排(dry-run/apply, 保留id, 重导+清洗)`

---

### Task 6: 迁移后验收(两次-sync 全跳过 + 分发正确 + 回滚链)

**Files:** Create `migrate/acceptance.md`(运维 runbook)+ `migrate/tests/test_acceptance_smoke.py`(可自动化的部分)

- [ ] **Step 1: 自动化 smoke**(在内存:apply 一批 fixture 旧数据 → 起 P1a TestClient → `GET /api/distribution/plugins?namespace=&service=`[带 pull token,需先给该 ns 设 pull_token]→断言返回 active 版本正确、version 非空、url 形如 download/{id};下载该 id→200)。
  - **B1 关键:smoke 至少用一个真实 `build --tar` .tgz 跑通**——取 `storage/tar/@business/plugin-mom-print-1.7.20.20260612134426.tgz`(或 `storage/tar/@business/` 下任一)做迁移源,走完「`fetch_bytes`→`reimport_version`→`store_tgz`→分发」,断言**分发返回的 version 非空且严格等于包内 `package.json.version`**(即把根级布局的 .tgz 真正解出版本)。这是「两次-sync 全跳过」验收的前置证据:只有真实根级布局能在 `parse_tgz` 不崩、版本正确,后续两次-sync 才可能相等。**不要只用合成 fixture .tgz**——它掩盖根级布局/真实 package.json 的问题。
- [ ] **Step 2: acceptance.md runbook**(真环境步骤,人工执行):
  1. 备份新库;`run.py --dry-run` 核汇总(尤其 version 改写数、冲突数)。
  2. `run.py --apply`。
  3. 给一个测试 namespace 轮换 pull token;把**一台测试节点**的 `sync-plugins.config.json` 的 adminUrl/adminToken(=pull token)/apiPath 指向新平台 `/api/distribution/plugins`。
  4. **第一次 sync**:节点应下载其 active 插件并启用。
  5. **第二次 sync(关键验收)**:必须**全部跳过、0 下载、0 pm enable**(证明 version 重导正确、与 disk package.json 严格相等)。
  6. 验回滚:平台对某 service+plugin 回滚→该节点下次 sync 拉到上一版本。
  7. 对账:旧平台 queryPlugin 与新平台 distribution 对同一 ns/service 返回的 (插件,版本) 集合一致。
- [ ] **Step 3:** smoke 绿。 **Step 4: commit** `feat(platform-migrate): 迁移后验收(两次-sync 全跳过 smoke + runbook)`

---

## Self-Review

- **覆盖 spec 数据迁移节**:迁 7 表(不迁 comment_record/print)✅;清洗(剔 agentKey/previousVersionId/action、isActive→bool+spv_active_key、唯一去重、单活规整)✅;**version 重导(High-2)**✅;包字节本地/S3 两路(M-5:local 拼到 filename;M-6:按 storages.type 判后端)✅;dry-run✅;只读旧库/勿碰 58✅。
- **两次-sync 全跳过验收 —— 尚未自证,依赖 B1 修复(评审)**:此前把它当「已验证 ✅」是虚标。它依赖 P1a `parse_tgz` 支持**根级布局**的修复(B1 主体在 P1a);本计划侧已落地前置条件——`make_tgz` fixture 用根级布局(Task 3 Step 1)+ Task 6 Step 1 smoke 强制跑**真实 `build --tar` .tgz** 验证「分发 version 非空且=包内 package.json.version」。只有这些先绿,两次-sync 相等性才有证据,runbook 的 Step 5 才可信。
- **依赖真实列**:映射字段名照 `docs/fields.sql` 旧列(namespaceCode/serviceCode/dir/image/action/url/isActive/isRolledBack/previousVersionId/versionOrder),非臆测;**已核到 `t_service_plugin` typo 列 `namesapceId`**(M-8 陷阱,映射须丢弃)。
- **映射防脏列(评审 M-8)**:`map_*` 一律逐字段白名单、禁 `{**old}`;每个 map_* 配「输出 keys ⊆ `inspect(Model).columns.keys()`」反向断言 + fixtures 带脏列(`namesapceId`/`createdById`/`agentKey`)使断言有证伪力。
- **附件不悬空(评审 M-7)**:dedup 后用同一 idmap 调 `remap_attachment_version_ids` 重映射 `plugin_attachment.plugin_version_id`;test_cleanse 含「dedup 后附件不悬空、保留 pv 恰一条」用例。
- **占位扫描**:纯函数均给测试用例;run.py 给完整流程步骤;runbook 给真环境 7 步。
- **类型一致**:`map_*`/`dedup_versions(返回 idmap)`/`remap_spv_version_ids`/`remap_attachment_version_ids`/`enforce_single_active`/`reimport_version`/`detect_backend(storage_row)`/`fetch_bytes(attachment_row, storage_row)` 跨任务签名一致;复用 P1a `app.storage.parse_tgz/store_tgz`、`app.db.database`、`app.db_models`。
- **遗留执行期定**:① 保留 id 插入后 MySQL `ALTER TABLE ... AUTO_INCREMENT`;② fetch_record 是否迁(默认否,可加开关)。
  - (原遗留①「旧包真实后端判定」已**升级为 Task 3 显式实现项**:old_reader 读 storages 表 + `detect_backend` 按 `storages.type` 判 + local 真实路径用例,不再留作执行期抽查。)
```
