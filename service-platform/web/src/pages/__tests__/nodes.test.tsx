import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import NodesPage from '../NodesPage';

// mock 资源层:节点页所有数据访问都走 ../../api/resources。
//  - listNodes        → 节点列表(服务端分页,信封 {count, rows, …};row = 后端 NodeOut camelCase)
//  - dispatchNodeAction(agentId, serviceCode, action, body) → 行级运维动作,捕获其入参做断言
const listNodes = vi.fn();
const dispatchNodeAction = vi.fn();
vi.mock('../../api/resources', () => ({
  listNodes: (...a: unknown[]) => listNodes(...a),
  dispatchNodeAction: (...a: unknown[]) => dispatchNodeAction(...a),
}));

// antd 在两个汉字按钮间插空格,带图标按钮的 accessible name 还含图标名;抹空白后用 includes 匹配。
const byNormalizedName = (text: string) => (name: string) => name.replace(/\s/g, '').includes(text);

// jsdom 不实现以下 API,antd Dropdown/Modal 的滚动锁/虚拟列表会调用,补最小 stub 保证可交互、用例稳定。
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

// 节点列表信封:严格对齐后端 NodeOut(camelCase),逐字段对齐契约、不伪造字段名/类型。
//  - 行 1(svc-online):在线、degraded=false、healthyCount=2 → 健康数显「2」。
//  - 行 2(svc-degraded):在线但 degraded=true → 健康数应显「-」(降级行不展示健康计数)。
// agentId === namespaceCode(命名空间列直接用 agentId);dir/defaultImage/nacosServiceName/lastSeen 按契约给值。
const nodesEnvelope = {
  count: 2,
  rows: [
    {
      agentId: 'ns-online',
      serviceCode: 'svc-online',
      namespaceCode: 'ns-online',
      dir: '/opt/svc-online',
      defaultImage: 'img-online:1',
      nacosServiceName: 'nacos-online',
      online: true,
      lastSeen: '2026-06-20T10:00:00Z',
      healthyCount: 2,
      degraded: false,
    },
    {
      agentId: 'ns-degraded',
      serviceCode: 'svc-degraded',
      namespaceCode: 'ns-degraded',
      dir: '/opt/svc-degraded',
      defaultImage: 'img-degraded:1',
      nacosServiceName: 'nacos-degraded',
      online: true,
      lastSeen: null,
      healthyCount: 5,
      degraded: true,
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

// 打开某一行的「操作」Dropdown,并点击其菜单项(按可见文案,菜单项渲染在 portal 里)。
const openRowMenu = async (user: ReturnType<typeof userEvent.setup>, rowText: string) => {
  const row = (await screen.findByText(rowText)).closest('tr')!;
  await user.click(within(row).getByRole('button', { name: byNormalizedName('操作') }));
};

// 点击 portal 菜单里文案完全匹配的项(antd menu item 文案在 .ant-dropdown-menu-title-content 内)。
const clickMenuItem = async (user: ReturnType<typeof userEvent.setup>, label: string) => {
  const item = await screen.findByText(
    (_t, node) =>
      node?.classList.contains('ant-dropdown-menu-title-content') === true &&
      node.textContent?.trim() === label,
  );
  await user.click(item);
};

describe('NodesPage', () => {
  beforeEach(() => {
    listNodes.mockReset();
    dispatchNodeAction.mockReset();
    listNodes.mockResolvedValue(nodesEnvelope);
  });

  it('渲染节点行:serviceCode / 在线状态 / 健康数;degraded 行健康数显「-」', async () => {
    render(<NodesPage />);

    // serviceCode 列。
    expect(await screen.findByText('svc-online')).toBeInTheDocument();
    expect(screen.getByText('svc-degraded')).toBeInTheDocument();

    // online=true → Tag「在线」(两行都在线,出现两次)。
    expect(screen.getAllByText('在线').length).toBeGreaterThanOrEqual(2);

    // 行 1:healthyCount=2 且未降级 → 显「2」。
    const onlineRow = screen.getByText('svc-online').closest('tr')!;
    expect(within(onlineRow).getByText('2')).toBeInTheDocument();

    // 行 2:degraded=true → 健康数显「-」(不展示 healthyCount=5)。
    const degradedRow = screen.getByText('svc-degraded').closest('tr')!;
    expect(within(degradedRow).getByText('-')).toBeInTheDocument();
    expect(within(degradedRow).queryByText('5')).not.toBeInTheDocument();

    // 服务端分页:走 listNodes(读统一信封),首屏即调一次。
    await waitFor(() => expect(listNodes).toHaveBeenCalled());
  });

  it('「停止(force)」→ 二次确认 Modal:输错 serviceCode 时确认禁用,输对后确认 → dispatchNodeAction(agentId, serviceCode, "stop", {mode:"force"})', async () => {
    dispatchNodeAction.mockResolvedValue({ kind: 'command', requestId: 'req-1', accepted: true });
    const user = userEvent.setup();
    render(<NodesPage />);

    expect(await screen.findByText('svc-online')).toBeInTheDocument();

    // 开行操作菜单 → 选「停止(force)」。
    await openRowMenu(user, 'svc-online');
    await clickMenuItem(user, '停止(force)');

    // 二次确认 Modal 出现:确认按钮初始禁用(未输入 serviceCode)。
    const confirmBtn = await screen.findByRole('button', { name: byNormalizedName('确认') });
    expect(confirmBtn).toBeDisabled();

    // 护栏定位输入框(Modal 内文本框)。
    const guard = await screen.findByPlaceholderText(byNormalizedName('serviceCode') as never);

    // 输错 → 仍禁用。
    await user.type(guard, 'wrong-code');
    expect(confirmBtn).toBeDisabled();

    // 改成正确 serviceCode → 可点。
    await user.clear(guard);
    await user.type(guard, 'svc-online');
    await waitFor(() => expect(confirmBtn).toBeEnabled());

    // 点确认 → dispatchNodeAction(agentId, serviceCode, 'stop', {mode:'force', allowLastInstance:false})。
    await user.click(confirmBtn);
    await waitFor(() =>
      expect(dispatchNodeAction).toHaveBeenCalledWith(
        'ns-online',
        'svc-online',
        'stop',
        expect.objectContaining({ mode: 'force' }),
      ),
    );
  });

  it('「启动」→ action="start" 且不带 mode', async () => {
    dispatchNodeAction.mockResolvedValue({ kind: 'command', requestId: 'req-2', accepted: true });
    const user = userEvent.setup();
    render(<NodesPage />);

    expect(await screen.findByText('svc-online')).toBeInTheDocument();
    await openRowMenu(user, 'svc-online');
    await clickMenuItem(user, '启动');

    // 输对 serviceCode → 确认。
    const confirmBtn = await screen.findByRole('button', { name: byNormalizedName('确认') });
    const guard = await screen.findByPlaceholderText(byNormalizedName('serviceCode') as never);
    await user.type(guard, 'svc-online');
    await waitFor(() => expect(confirmBtn).toBeEnabled());
    await user.click(confirmBtn);

    await waitFor(() => expect(dispatchNodeAction).toHaveBeenCalled());
    const [agentId, serviceCode, action, body] = dispatchNodeAction.mock.calls[0] as [
      string,
      string,
      string,
      Record<string, unknown> | undefined,
    ];
    expect(agentId).toBe('ns-online');
    expect(serviceCode).toBe('svc-online');
    expect(action).toBe('start');
    // start 无 mode:body 不含 mode 键。
    expect(body?.mode).toBeUndefined();
  });

  it('「重启(优雅)」→ action="restart" mode="graceful"', async () => {
    dispatchNodeAction.mockResolvedValue({ kind: 'rolling', taskId: 'task-9', accepted: true });
    const user = userEvent.setup();
    render(<NodesPage />);

    expect(await screen.findByText('svc-online')).toBeInTheDocument();
    await openRowMenu(user, 'svc-online');
    await clickMenuItem(user, '重启(优雅)');

    const confirmBtn = await screen.findByRole('button', { name: byNormalizedName('确认') });
    const guard = await screen.findByPlaceholderText(byNormalizedName('serviceCode') as never);
    await user.type(guard, 'svc-online');
    await waitFor(() => expect(confirmBtn).toBeEnabled());
    await user.click(confirmBtn);

    await waitFor(() =>
      expect(dispatchNodeAction).toHaveBeenCalledWith(
        'ns-online',
        'svc-online',
        'restart',
        expect.objectContaining({ mode: 'graceful' }),
      ),
    );
  });

  it('动作失败(409 无健康实例可优雅 drain)→ 有可见错误提示,不静默吞', async () => {
    dispatchNodeAction.mockRejectedValue({ response: { status: 409 } });
    const user = userEvent.setup();
    render(<NodesPage />);

    expect(await screen.findByText('svc-online')).toBeInTheDocument();
    await openRowMenu(user, 'svc-online');
    await clickMenuItem(user, '停止(优雅)');

    const confirmBtn = await screen.findByRole('button', { name: byNormalizedName('确认') });
    const guard = await screen.findByPlaceholderText(byNormalizedName('serviceCode') as never);
    await user.type(guard, 'svc-online');
    await waitFor(() => expect(confirmBtn).toBeEnabled());
    await user.click(confirmBtn);

    await waitFor(() => expect(dispatchNodeAction).toHaveBeenCalled());

    // A2:非 401 失败必须有可见兜底提示。匹配 409 专属 toast 的独有短语「drain」
    //（列头/菜单项也含「健康实例」「优雅」,故按 toast 独有词锁定,避免误中页面其它文本)。
    expect(await screen.findByText((t) => t.includes('drain'))).toBeInTheDocument();
  });
});
