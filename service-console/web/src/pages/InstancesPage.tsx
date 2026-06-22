import { useState } from 'react';
import { ProTable, type ProColumns } from '@ant-design/pro-components';
import { Button, Tag, Tooltip, Typography } from 'antd';
import { FileTextOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';
import type { InstanceRow } from '../api/resources';
import InstanceLogDrawer from '../components/InstanceLogDrawer';

const { Text } = Typography;

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
 * 行操作(本期仅「日志」,P3-9):点「日志」打开 {@link InstanceLogDrawer} 接 console 既有 SSE 看实时
 * tail(`agentId` + 发现权威 `dir`)。**仅当该行有 `dir` 时可点**(无 dir 禁用 + tooltip 说明);
 * 启动/停止/更新等运维操作仍留后续批次,本页不接。
 * 鉴权失败(401)由 client 拦截器统一跳登录;日志 SSE 的 401/403 由抽屉内显式报错(见组件注释)。
 */
export default function InstancesPage() {
  // 当前打开日志抽屉的目标实例(null = 关闭)。取该行 agentId + dir 发起 SSE。
  const [logTarget, setLogTarget] = useState<InstanceRow | null>(null);

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

  return (
    <>
    <ProTable<InstanceRow>
      // 行唯一键:agentId + containerName(后端 uq_dn_agent_container 唯一约束据此)。
      rowKey={(r) => `${r.agentId}:${r.containerName}`}
      // 展示列(模块级常量)+ 行操作列(需组件 state 开日志抽屉,故在组件内拼上)。
      columns={[...columns, optionColumn]}
      // 服务端分页:current/pageSize → 后端 page/pageSize;查询表单产生的 namespace/status 过滤平铺透传;
      // 空字符串过滤值剔除(不发空参,后端仅按 truthy 过滤);读信封后返回 ProTable 约定结构。
      request={async (params) => {
        const { current, pageSize, ...filter } = params;
        const cleaned: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(filter)) {
          if (v !== undefined && v !== null && v !== '') cleaned[k] = v;
        }
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
