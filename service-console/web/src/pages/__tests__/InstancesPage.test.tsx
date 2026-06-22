import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import InstancesPage from '../InstancesPage';

// mock 资源层:实例页只读,数据访问只走 ../../api/resources.listInstances
//   listInstances(params) → 实例列表(服务端分页,信封 {count, rows, …};row = 后端 DiscoveredNodeOut camelCase)
// 捕获其入参以断言:服务端分页(page/pageSize)、namespace/status 过滤透传、空值不透传。
const listInstances = vi.fn();
vi.mock('../../api/resources', () => ({
  listInstances: (...a: unknown[]) => listInstances(...a),
}));

// jsdom 不实现以下 API,ProTable / antd Select 的虚拟列表/滚动会调用,补最小 stub 保证用例稳定。
if (!HTMLElement.prototype.scrollIntoView) {
  HTMLElement.prototype.scrollIntoView = () => {};
}
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// antd 在两个汉字按钮间插空格,带图标按钮的 accessible name 还含图标名;抹空白后用 includes 匹配。
const byNormalizedName = (text: string) => (name: string) => name.replace(/\s/g, '').includes(text);

// 实例列表信封:严格对齐后端 DiscoveredNodeOut(camelCase),逐字段对齐契约、不伪造字段名/类型。
//  - 行 1(active-running):active + running + healthy=true → 状态「在报」、运行「运行中」、健康「健康」。
//  - 行 2(stale-stopped):stale + 停 + healthy=null → 状态「离线·stale」、运行「已停」、健康「-」、nacosService=null → 服务「-」。
// dir/image/composeProject 为发现权威值;heartbeatAt/firstSeenAt 按契约给 ISO8601 / null。
// 各列取值刻意互不相同(容器/工程/目录/镜像/服务各唯一),便于 getByText 精确锁列、不撞列。
const instancesEnvelope = {
  count: 2,
  rows: [
    {
      agentId: 'ns-online',
      containerName: 'container-admin',
      containerId: 'cid-1',
      composeProject: 'project-admin',
      composeService: 'svc-app',
      dir: '/data/orchidea/admin',
      image: 'oci.example.com/nocobase-pro:1.7.20',
      running: true,
      nacosService: 'wms-admin',
      healthy: true,
      status: 'active',
      heartbeatAt: '2026-06-20T10:00:00Z',
      firstSeenAt: '2026-06-01T08:00:00Z',
    },
    {
      agentId: 'ns-prod',
      containerName: 'container-prod-a1',
      containerId: null,
      composeProject: 'project-prod-a1',
      composeService: 'svc-app',
      dir: '/data/orchidea/prod/a1',
      image: 'oci.example.com/nocobase-pro:1.7.19',
      running: false,
      nacosService: null,
      healthy: null,
      status: 'stale',
      heartbeatAt: null,
      firstSeenAt: '2026-05-01T08:00:00Z',
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

const emptyEnvelope = { count: 0, rows: [], page: 1, pageSize: 20, totalPage: 0 };

// 打开某个筛选项 Select 并展开下拉(按 combobox id 定位,= dataIndex)。
const openSelect = async (user: ReturnType<typeof userEvent.setup>, fieldId: string) => {
  const combobox = await waitFor(() => {
    const el = document.getElementById(fieldId);
    if (!el) throw new Error(`combobox #${fieldId} 未渲染`);
    return el;
  });
  await user.click(combobox);
};

// 点选下拉中文案匹配的选项(选项渲染在 portal 的 .ant-select-item-option-content 内)。
const clickOption = async (user: ReturnType<typeof userEvent.setup>, contains: string) => {
  const option = await screen.findByText(
    (_t, node) =>
      node?.classList.contains('ant-select-item-option-content') === true &&
      node.textContent?.includes(contains) === true,
  );
  await user.click(option);
};

describe('InstancesPage', () => {
  beforeEach(() => {
    listInstances.mockReset();
    listInstances.mockResolvedValue(instancesEnvelope);
  });

  it('渲染实例行:camel 字段映射到列(命名空间/服务/容器/工程/目录/镜像)+ 运行/健康/状态徽章', async () => {
    render(<InstancesPage />);

    // 命名空间列 = agentId。
    expect(await screen.findByText('ns-online')).toBeInTheDocument();
    expect(screen.getByText('ns-prod')).toBeInTheDocument();

    // 容器列 = containerName;工程列 = composeProject;目录列 = dir;镜像列 = image。
    expect(screen.getByText('container-admin')).toBeInTheDocument();
    expect(screen.getByText('container-prod-a1')).toBeInTheDocument();
    expect(screen.getByText('project-admin')).toBeInTheDocument();
    expect(screen.getByText('/data/orchidea/admin')).toBeInTheDocument();
    expect(screen.getByText('oci.example.com/nocobase-pro:1.7.20')).toBeInTheDocument();

    // 服务列 = nacosService(蓝 tag);行 1 有匹配显服务名,行 2 无匹配显「-」。
    const onlineRow = screen.getByText('ns-online').closest('tr')!;
    expect(within(onlineRow).getByText('wms-admin')).toBeInTheDocument();

    // 行 1(active + running + healthy=true):状态「在报」、运行「运行中」、健康「健康」。
    expect(within(onlineRow).getByText('在报')).toBeInTheDocument();
    expect(within(onlineRow).getByText('运行中')).toBeInTheDocument();
    expect(within(onlineRow).getByText('健康')).toBeInTheDocument();

    // 行 2(stale + 停 + healthy=null):状态「离线·stale」、运行「已停」;healthy=null 与 nacosService=null → 「-」。
    const prodRow = screen.getByText('ns-prod').closest('tr')!;
    expect(within(prodRow).getByText('离线·stale')).toBeInTheDocument();
    expect(within(prodRow).getByText('已停')).toBeInTheDocument();
    // 该行至少出现两处「-」(nacosService 与 healthy 均空);不臆造健康/服务值。
    expect(within(prodRow).getAllByText('-').length).toBeGreaterThanOrEqual(2);

    // 服务端分页:首屏即调一次 listInstances。
    await waitFor(() => expect(listInstances).toHaveBeenCalled());
  });

  it('服务端分页:listInstances 入参含 page/pageSize(勿全量返回)', async () => {
    render(<InstancesPage />);
    await screen.findByText('ns-online');

    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    const params = listInstances.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(params.page).toBeDefined();
    expect(params.pageSize).toBeDefined();
  });

  it('纯只读:无「添加」按钮、无行内编辑/删除/运维操作', async () => {
    render(<InstancesPage />);
    await screen.findByText('ns-online');

    // 本期只读列表:不渲染添加 / 编辑 / 删除,也无行级运维动作(启动/停止/更新)。
    expect(screen.queryByText('添加')).not.toBeInTheDocument();
    expect(screen.queryByText('编辑')).not.toBeInTheDocument();
    expect(screen.queryByText('删除')).not.toBeInTheDocument();
    // 行内不出现运维动作按钮(本期不接)。
    const onlineRow = screen.getByText('ns-online').closest('tr')!;
    expect(within(onlineRow).queryByText('启动')).not.toBeInTheDocument();
    expect(within(onlineRow).queryByText('停止')).not.toBeInTheDocument();
    expect(within(onlineRow).queryByText('更新')).not.toBeInTheDocument();
  });

  it('按命名空间筛选:输入 namespace → 查询 → listInstances({namespace}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    render(<InstancesPage />);
    await screen.findByText('ns-online');

    // namespace 筛选是文本输入(agentId 自由串);id = dataIndex 'namespace'。
    const nsInput = (await waitFor(() => {
      const el = document.getElementById('namespace');
      if (!el) throw new Error('namespace 输入框未渲染');
      return el;
    })) as HTMLInputElement;
    await user.type(nsInput, 'ns-online');
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    // 关键:筛选值 namespace 透传到 listInstances({ namespace })(后端 ?namespace= 过滤)。
    await waitFor(() => {
      expect(listInstances).toHaveBeenCalledWith(expect.objectContaining({ namespace: 'ns-online' }));
    });
  });

  it('按状态筛选:选 stale → 查询 → listInstances({status:"stale"}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    render(<InstancesPage />);
    await screen.findByText('ns-online');

    // status 筛选下拉(valueEnum:active/stale);选「stale(失联)」。
    await openSelect(user, 'status');
    await clickOption(user, 'stale');
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    await waitFor(() => {
      expect(listInstances).toHaveBeenCalledWith(expect.objectContaining({ status: 'stale' }));
    });
  });

  it('空筛选值不透传:重置后再查询,listInstances 入参不含 namespace/status 空串', async () => {
    const user = userEvent.setup();
    render(<InstancesPage />);
    await screen.findByText('ns-online');

    // 先输 namespace 再清空,点查询:不应把 namespace='' 发给后端(后端仅按 truthy 过滤,空串会污染)。
    const nsInput = document.getElementById('namespace') as HTMLInputElement;
    await user.type(nsInput, 'x');
    await user.clear(nsInput);
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    // 任一次调用都不得带空串的 namespace / status。
    for (const call of listInstances.mock.calls) {
      const p = (call[0] ?? {}) as Record<string, unknown>;
      expect(p.namespace).not.toBe('');
      expect(p.status).not.toBe('');
    }
  });

  it('空态:后端回 0 行 → 不抛错,渲染空表(无数据行)', async () => {
    listInstances.mockResolvedValue(emptyEnvelope);
    render(<InstancesPage />);

    // 列头仍在(表渲染成功),但没有任何实例数据行。
    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    expect(screen.queryByText('ns-online')).not.toBeInTheDocument();
    // antd 空态占位文案(ProTable 在表体与查询表单各渲一处 Empty,故用 getAllByText 取至少一处)。
    await waitFor(() => expect(screen.getAllByText('暂无数据').length).toBeGreaterThanOrEqual(1));
  });

  it('错误态:请求失败 → 不崩(无数据行),仍调用了 listInstances(全局兜底 toast 由 client 拦截器处理)', async () => {
    listInstances.mockRejectedValue({ response: { status: 500 } });
    render(<InstancesPage />);

    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    // 失败时不渲染数据行;页面不抛(ProTable request 失败走 success:false)。
    expect(screen.queryByText('ns-online')).not.toBeInTheDocument();
  });
});
