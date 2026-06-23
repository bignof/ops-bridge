import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import RollingTaskNodesTable from '../RollingTaskNodesTable';
import type { RollingTask } from '../../api/resources';

// 可复用「逐实例进度」小表的单测:覆盖空态(task=null / nodes 空)与五状态 Tag + 实例/容器/错误渲染。
// (供 RolloutsPage 详情 Drawer 与 P4-5 发布弹窗复用,故单独测其呈现契约。)

const task = (nodes: RollingTask['nodes']): RollingTask => ({
  taskId: 't1',
  agentId: '*',
  serviceName: 'svc',
  status: 'running',
  degraded: false,
  nodes,
  error: null,
  createdAt: null,
  updatedAt: null,
});

describe('RollingTaskNodesTable(逐实例进度小表)', () => {
  it('task=null → 占位「暂无滚动进度」', () => {
    render(<RollingTaskNodesTable task={null} />);
    expect(screen.getByText('暂无滚动进度')).toBeInTheDocument();
  });

  it('nodes 空 → 占位「暂无滚动进度」', () => {
    render(<RollingTaskNodesTable task={task([])} />);
    expect(screen.getByText('暂无滚动进度')).toBeInTheDocument();
  });

  it('渲染五状态 Tag + 实例 address / 容器 / 失败 error', () => {
    render(
      <RollingTaskNodesTable
        task={task([
          { address: '10.0.0.1:1', containerId: 'c1', status: 'pending' },
          { address: '10.0.0.2:1', containerId: 'c2', status: 'in-progress' },
          { address: '10.0.0.3:1', containerId: 'c3', status: 'done' },
          { address: '10.0.0.4:1', containerId: 'c4', status: 'failed', error: '超时' },
          { address: '10.0.0.5:1', containerId: null, status: 'skipped' },
        ])}
      />,
    );

    // 五状态各自的中文 Tag。
    expect(screen.getByText('待滚')).toBeInTheDocument();
    expect(screen.getByText('滚动中')).toBeInTheDocument();
    expect(screen.getByText('完成')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.getByText('跳过')).toBeInTheDocument();

    // 实例 address 列。
    expect(screen.getByText('10.0.0.1:1')).toBeInTheDocument();
    // 失败实例 error。
    expect(screen.getByText('超时')).toBeInTheDocument();
    // containerId 为 null 的行容器列显「-」(至少一处)。
    expect(screen.getAllByText('-').length).toBeGreaterThanOrEqual(1);
  });

  it('node 带 agentId → 同时渲染 agentId 与 address', () => {
    render(
      <RollingTaskNodesTable
        task={task([{ agentId: 'ns-a', address: '10.0.0.9:1', containerId: 'c9', status: 'done' }])}
      />,
    );
    expect(screen.getByText('ns-a')).toBeInTheDocument();
    expect(screen.getByText('10.0.0.9:1')).toBeInTheDocument();
  });
});
