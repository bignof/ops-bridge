# docs/archive — 历史文档(已归档,仅供追溯)

这里是**已被取代 / 已完成阶段**的设计与计划文档,从 `docs/` 根移入(2026-06-22 整理)。**不再维护**,仅供追溯。当前活跃文档见 `docs/` 根。

## 当前活跃文档(在 `docs/` 根,不在此目录)
- `plugin-distribution-dev-plan.zh-CN.md` —— **唯一主计划**(P0 + M 合并 S1–S8 + P1–P5,接下来要执行的)。
- `plugin-distribution-redesign.zh-CN.md` / `.html` —— 设计冻结基线(含 2026-06-22 合并覆盖性声明)。
- `plugin-platform-prototype.zh-CN.html` —— 平台原型(UI 参考)。
- `review-ultracode-2026-06-22.zh-CN.md` —— 最近一轮评审(已应用进上述计划/设计/原型)。
- `*.sql`(collections/fields/uiSchemas)—— NocoBase 旧 schema dump,SPA 字段基线的源数据(保留作参考)。

## 归档清单与原因
- `2026-06-18-zero-downtime-rolling-restart-design.md` + `2026-06-18-rolling-restart-plan-{1-agent,2-hub,3-platform}.md` —— 早期「零中断滚动重启」设计/计划,已并入 node-control 与本轮 redesign。
- `2026-06-18-service-platform-design.md` + `2026-06-19-service-platform-plan-{p1-deploy-isolation,p1-spa-frontend,p1a-backend,p1b-migration}.md` —— service-platform 一期(P1)设计与计划,**platform 已并入 service-console**(见主计划 M 阶段),拓扑已变。
- `2026-06-20-node-control-{design,plan}.md` —— node-control v3 设计/计划;其「Service 表权威源」模型**已被 redesign §3.3 推翻**(改 DiscoveredNode 发现权威)。
- `ARCHITECTURE.md` / `ROADMAP.md` —— 合并前(hub+platform 两服务)的仓库级架构/路线,**待 console 合并完成后重写**。
- `PHASE1_ACCEPTANCE.md` / `PHASE1_BASELINE.md` —— 一期范围/验收记录。
- `ui-baseline-p1.md` —— service-platform P1-SPA 前端冻结基线(派生自 `../*.sql`);其涉及的服务两管理面 UI 已并入主计划 P3-8 / P4-4 / P4-5。
- `review-ultracode-2026-06-21.zh-CN.md` —— 第一轮 ultracode 评审(原型+设计),结论已全部应用,后由 06-22 轮接替。
