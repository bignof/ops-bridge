import { ProTable, type ProColumns } from '@ant-design/pro-components';
import { Tag, Typography } from 'antd';
import * as resources from '../api/resources';
import type { NodeOperationRow } from '../api/resources';

const { Text } = Typography;

// status → Tag 颜色:成功绿 / 失败红 / 进行中蓝 / 其余默认灰(未知值原样显示)。
// 取值来自 hub CommandSnapshot.status(本端点代理 hub /api/commands),实证取值含 succeeded/queued/
// error 等(见后端 test_nodes 审计用例 status="succeeded");不臆造未见过的别名,未命中即走默认色。
const GREEN_STATUSES = new Set(['success', 'succeeded']);
const RED_STATUSES = new Set(['failed', 'error']);
const BLUE_STATUSES = new Set(['queued', 'pending', 'processing', 'running']);
const statusColor = (status: string): string | undefined => {
  const s = status.toLowerCase();
  if (GREEN_STATUSES.has(s)) return 'green';
  if (RED_STATUSES.has(s)) return 'red';
  if (BLUE_STATUSES.has(s)) return 'blue';
  return undefined;
};

// 可空文本列统一渲染:null/undefined/空串 → 「-」,否则原值。
const dash = (v?: string | null) => (v ? v : '-');

// 列(纯只读审计,无任何行操作 / 写操作):
//  who(操作人=requestedBy 派生身份) / action / mode / 目标目录(dir) / status(Tag)
//  / 来源(requestSource) / 时间(createdAt) / 输出摘要(output,省略 + Tooltip)。
const columns: ProColumns<NodeOperationRow>[] = [
  { title: '请求 ID', dataIndex: 'requestId', key: 'requestId', copyable: true, width: 180 },
  { title: '节点', dataIndex: 'agentId', key: 'agentId', width: 140 },
  { title: '动作', dataIndex: 'action', key: 'action', width: 120 },
  // mode 可空(start 无 mode)→ 「-」。
  { title: '模式', dataIndex: 'mode', key: 'mode', width: 100, render: (_dom, r) => dash(r.mode) },
  {
    title: '状态',
    dataIndex: 'status',
    key: 'status',
    width: 110,
    render: (_dom, r) => <Tag color={statusColor(r.status)}>{r.status}</Tag>,
  },
  // 操作人 = 后端派生身份 requestedBy(谁下发);可空 → 「-」。
  {
    title: '操作人',
    dataIndex: 'requestedBy',
    key: 'requestedBy',
    width: 120,
    render: (_dom, r) => dash(r.requestedBy),
  },
  // 来源 requestSource 可空 → 「-」。
  {
    title: '来源',
    dataIndex: 'requestSource',
    key: 'requestSource',
    width: 120,
    render: (_dom, r) => dash(r.requestSource),
  },
  // 目标目录 dir 可空 → 「-」;长路径省略。
  {
    title: '目标目录',
    dataIndex: 'dir',
    key: 'dir',
    width: 200,
    ellipsis: true,
    render: (_dom, r) => dash(r.dir),
  },
  // 时间:后端回 ISO8601(带 T/Z),用 valueType 'dateTime' 经 dayjs 格式化(空值 ProTable 自动显「-」)。
  { title: '时间', dataIndex: 'createdAt', key: 'createdAt', valueType: 'dateTime', width: 170 },
  // 输出摘要 output:后端已截尾;列宽受限 + 单元格内 Text ellipsis(hover Tooltip 看全文),空 → 「-」。
  // 不直接整段平铺,避免长文本撑爆表格。
  {
    title: '输出',
    dataIndex: 'output',
    key: 'output',
    width: 280,
    render: (_dom, r) =>
      r.output ? (
        <Text style={{ maxWidth: 260 }} ellipsis={{ tooltip: r.output }}>
          {r.output}
        </Text>
      ) : (
        '-'
      ),
  },
];

/**
 * 操作审计页(resource `node-operations`):**纯只读**展示 hub dispatch 命令审计
 * (start/stop/force-restart/redeploy)。优雅 restart 走 rolling,不在此列表(已知缺口)。
 *
 * 记录由 hub 派发命令时产生,**非 UI 创建**,故本页无任何增删改 / 行操作动作。
 *
 * 审计行数无界,**必须服务端分页**:ProTable `request` 把 antd 的 `current`/`pageSize`
 * 映射为后端 `page`/`pageSize`,读统一信封 `{count, rows, …}` 后返回 ProTable 约定结构,
 * **勿全量返回**。鉴权失败(401)由 client 拦截器统一跳登录。
 */
export default function NodeOperationsPage() {
  return (
    <ProTable<NodeOperationRow>
      rowKey="requestId"
      columns={columns}
      // 服务端分页:current/pageSize → 后端 page/pageSize;读统一信封后返回 ProTable 约定结构。
      request={async (params) => {
        const { current, pageSize } = params;
        const env = await resources.listNodeOperations<NodeOperationRow>({
          page: current,
          pageSize,
        });
        return { data: env.rows, total: env.count, success: true };
      }}
      pagination={{ showSizeChanger: true }}
      // 纯只读:无查询表单(本页列无服务端过滤键),无添加 / 编辑 / 行操作。
      search={false}
      options={{ reload: true, density: false, setting: false }}
      toolBarRender={false}
      dateFormatter="string"
      scroll={{ x: 'max-content' }}
    />
  );
}
