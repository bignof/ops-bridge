import { Empty, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { RollingTask, RollingTaskNode } from '../api/resources';

const { Text } = Typography;

// 逐实例滚动进度状态 → Tag(对齐 hub 协调器 nodes[].status 五值):
//  - pending     待滚(灰)
//  - in-progress 滚动中(蓝)
//  - done        完成(绿)
//  - failed      失败(红)
//  - skipped     跳过(默认灰,弱化)
// 未知值原样显示(默认灰),不臆造别名。
const renderNodeStatus = (status: RollingTaskNode['status']) => {
  switch (status) {
    case 'pending':
      return <Tag>待滚</Tag>;
    case 'in-progress':
      return <Tag color="blue">滚动中</Tag>;
    case 'done':
      return <Tag color="green">完成</Tag>;
    case 'failed':
      return <Tag color="red">失败</Tag>;
    case 'skipped':
      return <Tag>跳过</Tag>;
    default:
      return <Tag>{status}</Tag>;
  }
};

// 逐实例进度小表的列:实例(agentId/address)/ 容器 / 状态 / 错误。
// 实例标识:跨机滚动以 address(host:port)为准;agentId 仅部分场景带,有则附在前。
const columns: ColumnsType<RollingTaskNode> = [
  {
    title: '实例',
    key: 'instance',
    render: (_v, r) =>
      r.agentId ? (
        <span>
          <Text type="secondary">{r.agentId}</Text> / <Text code>{r.address}</Text>
        </span>
      ) : (
        <Text code>{r.address}</Text>
      ),
  },
  {
    title: '容器',
    dataIndex: 'containerId',
    key: 'containerId',
    render: (_v, r) =>
      r.containerId ? (
        <Text style={{ maxWidth: 200 }} ellipsis={{ tooltip: r.containerId }} copyable>
          {r.containerId}
        </Text>
      ) : (
        '-'
      ),
  },
  {
    title: '状态',
    dataIndex: 'status',
    key: 'status',
    render: (_v, r) => renderNodeStatus(r.status),
  },
  {
    title: '错误',
    dataIndex: 'error',
    key: 'error',
    render: (_v, r) =>
      r.error ? (
        <Text type="danger" style={{ maxWidth: 280 }} ellipsis={{ tooltip: r.error }}>
          {r.error}
        </Text>
      ) : (
        '-'
      ),
  },
];

/**
 * 底层滚动任务「逐实例进度」小表(投放 → rollingTask 逐实例的可观测呈现)。
 *
 * 抽成可复用组件:供 RolloutsPage 详情 Drawer 用,亦供 P4-5 发布弹窗发起投放后内嵌实时进度复用。
 * 入参 `task` 即 {@link RollingTask}(后端 RolloutDetailOut.rollingTask,无关联滚动时为 null);
 * 为 null / nodes 空 → 占位「暂无滚动进度」(纯展示,不自带拉取/轮询,由调用方传入数据并刷新)。
 */
export default function RollingTaskNodesTable({ task }: { task: RollingTask | null }) {
  if (!task || !task.nodes || task.nodes.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无滚动进度" />;
  }
  return (
    <Table<RollingTaskNode>
      // 行唯一键:address(跨机滚动每实例寻址唯一);缺失则退化用 containerId / 下标兜底。
      rowKey={(r, idx) => r.address || r.containerId || String(idx)}
      columns={columns}
      dataSource={task.nodes}
      pagination={false}
      size="small"
    />
  );
}
