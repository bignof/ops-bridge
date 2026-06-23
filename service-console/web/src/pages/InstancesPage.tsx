import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ProTable,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import { Alert, Button, Segmented, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { FileTextOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';
import type { InstanceRow } from '../api/resources';
import InstanceLogDrawer from '../components/InstanceLogDrawer';
import { useNamespace } from '../context/NamespaceContext';

const { Text } = Typography;

// 「全部命名空间」下按服务聚合的 pageSize 上限:取后端各 list 端点硬卡值 200(>200 会 422)。
// 聚合在前端按 nacosService 汇总这批实例;>200 实例的部署聚合会截断(已知边界,与 service-plugins
// 版本 join 同源限制,需后端支持聚合端点后再放开,不臆造端点)。
const AGGREGATE_PAGE_SIZE = 200;

// 跨命名空间「按服务聚合」一行:同一 nacosService 跨机汇总(实例数 / 机器数 / 健康数)。
interface ServiceAggregateRow {
  nacosService: string;
  /** 该服务的发现实例总数(跨机合计)。 */
  instanceCount: number;
  /** 承载该服务的不同机器(agentId)数。 */
  machineCount: number;
  /** healthy=true 的实例数(nacos 健康)。 */
  healthyCount: number;
}

// 把一批发现实例按 nacosService 聚合成跨机汇总行(nacosService 为空的实例归入「(未匹配 nacos)」占位组,
// 仍可见、不丢)。按实例数降序排,运维一眼看到「哪个服务铺得最多、在几台机器上」。
const aggregateByService = (rows: InstanceRow[]): ServiceAggregateRow[] => {
  const map = new Map<string, { instances: number; machines: Set<string>; healthy: number }>();
  for (const r of rows) {
    const key = r.nacosService || '(未匹配 nacos)';
    let agg = map.get(key);
    if (!agg) {
      agg = { instances: 0, machines: new Set<string>(), healthy: 0 };
      map.set(key, agg);
    }
    agg.instances += 1;
    agg.machines.add(r.agentId);
    if (r.healthy === true) agg.healthy += 1;
  }
  return Array.from(map.entries())
    .map(([nacosService, agg]) => ({
      nacosService,
      instanceCount: agg.instances,
      machineCount: agg.machines.size,
      healthyCount: agg.healthy,
    }))
    .sort((a, b) => b.instanceCount - a.instanceCount);
};

// 状态徽章(对齐原型实例页 status 列三态,见 docs/plugin-platform-prototype.zh-CN.html `renderNodes`):
//  - stale  → 灰(默认)「离线·stale」:agent 失联 / 本轮缺席,台账保留可定位(M8,仍可被 start)。
//  - active → 蓝「在报」:本轮在发现上报中(running/healthy 由专列单独展示)。
// 未知值原样显示(走默认灰),不臆造别名。
const renderStatusTag = (status: string) => {
  if (status === 'stale') return <Tag>离线·stale</Tag>;
  if (status === 'active') return <Tag color="blue">在报</Tag>;
  return <Tag>{status}</Tag>;
};

// 运行徽章(running:容器是否在跑;docker 含 stopped 也上报)。原型「已停」用 t-warn(黄) → gold。
const renderRunningTag = (running: boolean) =>
  running ? <Tag color="green">运行中</Tag> : <Tag color="gold">已停</Tag>;

// 健康徽章(healthy:nacos 匹配的健康态;无匹配为 null → 「-」)。
const renderHealthyTag = (healthy: boolean | null) => {
  if (healthy === null || healthy === undefined) return '-';
  return healthy ? <Tag color="green">健康</Tag> : <Tag color="gold">不健康</Tag>;
};

// 可空文本列统一渲染:null/undefined/空串 → 「-」,否则原值。
const dash = (v?: string | null) => (v ? v : '-');

// status 筛选下拉枚举(默认不选 = 全部,含 active+stale;stale 也要可见以便 start)。
// valueEnum 让 ProTable 查询表单渲染为本地 Select(无需远程 request),选项文案贴近含义。
const STATUS_VALUE_ENUM = {
  active: { text: 'active(在报)' },
  stale: { text: 'stale(失联)' },
};

// 列(对照原型实例页 thead:命名空间 / 服务 / 容器 / 目录 / 镜像 / 状态;
// 另按 brief 显式拆出 工程(composeProject)/ 运行(running)/ 健康(healthy)为独立列)。
// 展示列一律 `search: false`(非后端过滤键);筛选只有 namespace / status 两个仅查询列(hideInTable)。
const columns: ProColumns<InstanceRow>[] = [
  // ── 仅查询用筛选列(hideInTable):dataIndex 直接对齐后端过滤参数(本页无表单,无 id 撞键之虞)──
  // namespace = agentId(发现实例的命名空间);agentId 是发现上报的自由字符串,用文本输入而非下拉
  // (不耦合命名空间台账;发现可能含未登记 agentId)。透传后端 ?namespace=。
  {
    title: '命名空间',
    dataIndex: 'namespace',
    key: 'namespace',
    hideInTable: true,
    valueType: 'text',
    fieldProps: { placeholder: '按命名空间(agentId)筛选' },
  },
  // status 筛选(active/stale);不选 = 全部(含 stale)。透传后端 ?status=。
  {
    title: '状态',
    dataIndex: 'status',
    key: 'status',
    hideInTable: true,
    valueType: 'select',
    valueEnum: STATUS_VALUE_ENUM,
    fieldProps: { placeholder: '全部(含 stale)', allowClear: true },
  },

  // ── 展示列(均 search:false;权威值 dir/image/composeProject 来自 agent 发现)──
  { title: '命名空间', dataIndex: 'agentId', key: 'agentId', search: false },
  // 服务 = nacos 匹配到的服务名;原型用蓝 tag 标。无匹配为 null → 「-」。
  {
    title: '服务',
    dataIndex: 'nacosService',
    key: 'nacosService',
    search: false,
    render: (_dom, r) => (r.nacosService ? <Tag color="blue">{r.nacosService}</Tag> : '-'),
  },
  { title: '容器', dataIndex: 'containerName', key: 'containerName', search: false, copyable: true },
  // compose 工程(发现权威);可空 → 「-」。
  {
    title: '工程',
    dataIndex: 'composeProject',
    key: 'composeProject',
    search: false,
    render: (_dom, r) => dash(r.composeProject),
  },
  // 目录(发现权威);长路径省略,空 → 「-」。
  {
    title: '目录',
    dataIndex: 'dir',
    key: 'dir',
    search: false,
    ellipsis: true,
    render: (_dom, r) => dash(r.dir),
  },
  // 镜像(发现权威);长串省略 + hover Tooltip 看全文,空 → 「-」。
  {
    title: '镜像',
    dataIndex: 'image',
    key: 'image',
    search: false,
    render: (_dom, r) =>
      r.image ? (
        <Text style={{ maxWidth: 260 }} ellipsis={{ tooltip: r.image }}>
          {r.image}
        </Text>
      ) : (
        '-'
      ),
  },
  // 运行(running:容器在跑/已停)。
  {
    title: '运行',
    dataIndex: 'running',
    key: 'running',
    search: false,
    render: (_dom, r) => renderRunningTag(r.running),
  },
  // 健康(healthy:nacos 健康态;null → 「-」)。
  {
    title: '健康',
    dataIndex: 'healthy',
    key: 'healthy',
    search: false,
    render: (_dom, r) => renderHealthyTag(r.healthy),
  },
  // 状态(active/stale 徽章)。
  {
    title: '状态',
    dataIndex: 'instanceStatus',
    key: 'instanceStatus',
    search: false,
    render: (_dom, r) => renderStatusTag(r.status),
  },
  // 心跳时间:后端回 ISO8601(带 T/Z),用 valueType 'dateTime' 经 dayjs 格式化(空值 ProTable 自动显「-」)。
  { title: '心跳时间', dataIndex: 'heartbeatAt', key: 'heartbeatAt', valueType: 'dateTime', search: false },
];

/**
 * 实例页(resource `nodes/instances`):agent 自动发现上报的容器实例(DiscoveredNode)**只读列表**。
 *
 * 与「节点」页(平台 Service 台账逻辑视图)不同:本页一行 = agent 真实发现的一个 compose 容器,
 * `dir`/`image`/`composeProject` 为**发现权威值**(非手配)。同一 nacos 名的多容器各算一个实例。
 *
 * 列表:服务端分页(ProTable `request` 映射 current/pageSize → 后端 page/pageSize,读统一信封)。
 * 筛选:namespace(=agentId,文本)、status(active/stale,**默认全部含 stale**);空值不透传(避免发
 *       `namespace=""` / `status=""` 给后端)。
 *
 * P3-10 顶栏命名空间切换器联动:
 *  - 选具体命名空间 → 以全局 ns(code=agentId)为准强制过滤,**覆盖**本页 namespace 筛选列(两者不打架);
 *    ProTable 经 `params` 感知全局 ns 变更自动重拉。
 *  - 选「全部命名空间」→ 额外提供「按服务聚合」视图(评审 L-7 闭环):跨机同名服务(同 nacosService)
 *    不被 ns 切散,聚合成 nacosService → 实例数 / 机器数 / 健康数 的总览小表(总览↔下钻闭环)。
 *
 * 行操作(本期仅「日志」,P3-9):点「日志」打开 {@link InstanceLogDrawer} 接 console 既有 SSE 看实时
 * tail(`agentId` + 发现权威 `dir`)。**仅当该行有 `dir` 时可点**(无 dir 禁用 + tooltip 说明);
 * 启动/停止/更新等运维操作仍留后续批次,本页不接。
 * 鉴权失败(401)由 client 拦截器统一跳登录;日志 SSE 的 401/403 由抽屉内显式报错(见组件注释)。
 */
export default function InstancesPage() {
  // 当前打开日志抽屉的目标实例(null = 关闭)。取该行 agentId + dir 发起 SSE。
  const [logTarget, setLogTarget] = useState<InstanceRow | null>(null);

  // P3-10:全局命名空间选中态(null = 全部命名空间)。选具体 ns 时按其 code(=agentId)强制过滤。
  const { namespace } = useNamespace();
  const actionRef = useRef<ActionType>();

  // 视图模式(仅「全部命名空间」下可切):list 明细 / aggregate 按服务聚合。
  // 选了具体 ns 即下钻 → 强制 list(聚合是「全部」总览的语义,见类头)。
  const [viewMode, setViewMode] = useState<'list' | 'aggregate'>('list');
  const effectiveMode: 'list' | 'aggregate' = namespace === null ? viewMode : 'list';

  // 全局 ns 变更时:重拉列表(ProTable params 变更会自动触发,这里 actionRef 兜底显式 reload)。
  useEffect(() => {
    actionRef.current?.reload();
  }, [namespace]);

  // ── 按服务聚合(仅 effectiveMode==='aggregate' 时拉)──────────────────────────────
  const [aggRows, setAggRows] = useState<ServiceAggregateRow[]>([]);
  const [aggLoading, setAggLoading] = useState(false);
  const [aggErrored, setAggErrored] = useState(false);

  // 拉一批发现实例(≤200)按 nacosService 聚合。仅在「全部命名空间 + 聚合视图」下取,默认含 active+stale
  // (不传 status);全局已是「全部」故不带 namespace。失败置错误态(全局兜底 toast 已由拦截器弹)。
  const fetchAggregate = useCallback(async () => {
    setAggLoading(true);
    setAggErrored(false);
    try {
      const env = await resources.listInstances<InstanceRow>({ page: 1, pageSize: AGGREGATE_PAGE_SIZE });
      setAggRows(aggregateByService(env.rows));
    } catch {
      setAggErrored(true);
      setAggRows([]);
    } finally {
      setAggLoading(false);
    }
  }, []);

  useEffect(() => {
    if (effectiveMode === 'aggregate') void fetchAggregate();
  }, [effectiveMode, fetchAggregate]);

  // 聚合表列:服务(nacos)/ 实例数 / 机器数 / 健康数(健康<实例时标黄提示有不健康实例)。
  const aggregateColumns: ColumnsType<ServiceAggregateRow> = useMemo(
    () => [
      {
        title: '服务(nacos)',
        dataIndex: 'nacosService',
        key: 'nacosService',
        render: (_v, r) => <Tag color="blue">{r.nacosService}</Tag>,
      },
      { title: '实例数', dataIndex: 'instanceCount', key: 'instanceCount' },
      { title: '机器数', dataIndex: 'machineCount', key: 'machineCount' },
      {
        title: '健康数',
        dataIndex: 'healthyCount',
        key: 'healthyCount',
        render: (_v, r) =>
          r.healthyCount < r.instanceCount ? (
            <Text type="warning">
              {r.healthyCount}/{r.instanceCount}
            </Text>
          ) : (
            <Text>
              {r.healthyCount}/{r.instanceCount}
            </Text>
          ),
      },
    ],
    [],
  );

  // 行操作列(仅「日志」):无 dir 的实例禁用(发现未取到工程目录,无法定位 compose 日志)。
  const optionColumn: ProColumns<InstanceRow> = {
    title: '操作',
    valueType: 'option',
    key: 'option',
    fixed: 'right',
    render: (_dom, record) => {
      const canViewLog = !!record.dir;
      const btn = (
        <Button
          size="small"
          icon={<FileTextOutlined />}
          disabled={!canViewLog}
          onClick={() => setLogTarget(record)}
        >
          日志
        </Button>
      );
      // 无 dir:禁用并用 tooltip 说明原因(disabled 按钮自身不触发 hover,故包一层 span 承载 tooltip)。
      return canViewLog ? (
        btn
      ) : (
        <Tooltip title="该实例未发现工程目录(dir),无法拉取实时日志">
          <span>{btn}</span>
        </Tooltip>
      );
    },
  };

  // 「全部命名空间」下的视图切换器 + 说明(选具体 ns 时不显,已是下钻明细)。
  const modeToolbar =
    namespace === null ? (
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Space>
          <Text type="secondary">视图:</Text>
          <Segmented<'list' | 'aggregate'>
            value={viewMode}
            onChange={(v) => setViewMode(v)}
            options={[
              { label: '实例明细', value: 'list' },
              { label: '按服务聚合', value: 'aggregate' },
            ]}
          />
        </Space>
        {effectiveMode === 'aggregate' ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            跨命名空间按 nacos 服务聚合:同名服务在多机/多命名空间的实例合并计数,一眼看清「这个服务在几台机器上有几个实例」。
            (聚合取最多 {AGGREGATE_PAGE_SIZE} 个实例,超出部分暂未计入。)
          </Text>
        ) : null}
      </Space>
    ) : null;

  return (
    <>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {modeToolbar}

        {effectiveMode === 'aggregate' ? (
          // 跨 ns 服务聚合总览小表(不分页,源数据 ≤200)。空/错误态给占位与重试。
          aggErrored ? (
            <Alert
              type="error"
              showIcon
              message="加载聚合数据失败"
              description="请检查网络或稍后重试。"
              action={
                <Button size="small" onClick={() => void fetchAggregate()}>
                  重试
                </Button>
              }
            />
          ) : (
            <Table<ServiceAggregateRow>
              rowKey={(r) => r.nacosService}
              columns={aggregateColumns}
              dataSource={aggRows}
              loading={aggLoading}
              pagination={false}
              size="middle"
            />
          )
        ) : (
          <ProTable<InstanceRow>
            actionRef={actionRef}
            // 行唯一键:agentId + containerName(后端 uq_dn_agent_container 唯一约束据此)。
            rowKey={(r) => `${r.agentId}:${r.containerName}`}
            // 展示列(模块级常量)+ 行操作列(需组件 state 开日志抽屉,故在组件内拼上)。
            columns={[...columns, optionColumn]}
            // P3-10:全局 ns 经 params 透传,变更即触发 ProTable 重拉(空串=全部,request 内剔除)。
            params={{ globalNamespace: namespace?.code ?? '' }}
            // 服务端分页:current/pageSize → 后端 page/pageSize;查询表单产生的 namespace/status 过滤平铺透传;
            // 空字符串过滤值剔除(不发空参,后端仅按 truthy 过滤);读信封后返回 ProTable 约定结构。
            request={async (params) => {
              const { current, pageSize, globalNamespace, ...filter } = params as typeof params & {
                globalNamespace?: string;
              };
              const cleaned: Record<string, unknown> = {};
              for (const [k, v] of Object.entries(filter)) {
                if (v !== undefined && v !== null && v !== '') cleaned[k] = v;
              }
              // 全局命名空间优先:选了具体 ns 则以其 code(=agentId)强制覆盖本页 namespace 筛选列。
              if (globalNamespace) cleaned.namespace = globalNamespace;
              try {
                const env = await resources.listInstances<InstanceRow>({
                  page: current,
                  pageSize,
                  ...cleaned,
                });
                return { data: env.rows, total: env.count, success: true };
              } catch {
                // 失败时返回 success:false(ProTable 走空态),不让 reject 冒泡成 unhandled rejection;
                // 全局兜底 toast 已由 client 拦截器统一弹出(本页只读,无需本地精确提示)。
                return { data: [], total: 0, success: false };
              }
            }}
            pagination={{ showSizeChanger: true }}
            // 开查询表单(namespace / status 筛选)。本期行操作仅「日志」(见 optionColumn)。
            search={{ labelWidth: 'auto', defaultCollapsed: false }}
            options={{ reload: true, density: false, setting: false }}
            toolBarRender={false}
            dateFormatter="string"
            scroll={{ x: 'max-content' }}
          />
        )}
      </Space>

      {/* 实时日志抽屉:目标实例的 agentId + 发现权威 dir 发起 SSE;关闭即 abort 停流。 */}
      <InstanceLogDrawer
        open={logTarget !== null}
        onClose={() => setLogTarget(null)}
        agentId={logTarget?.agentId ?? null}
        dir={logTarget?.dir ?? null}
        title={logTarget?.containerName}
      />
    </>
  );
}
