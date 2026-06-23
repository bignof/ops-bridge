import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RolloutsPage from '../RolloutsPage';
import { NamespaceContext, type SelectedNamespace } from '../../context/NamespaceContext';

// mock 资源层:投放记录页数据访问走 ../../api/resources。
//  - listRollouts(params)        → 投放记录列表(服务端分页,信封;row = RolloutOut camelCase)
//  - getRollout(id)              → 投放详情(含 rollingTask 逐实例进度)
//  - retryRollout(id) / rollbackRollout(id) → 失败处置,捕获入参做断言
const listRollouts = vi.fn();
const getRollout = vi.fn();
const retryRollout = vi.fn();
const rollbackRollout = vi.fn();
vi.mock('../../api/resources', () => ({
  listRollouts: (...a: unknown[]) => listRollouts(...a),
  getRollout: (...a: unknown[]) => getRollout(...a),
  retryRollout: (...a: unknown[]) => retryRollout(...a),
  rollbackRollout: (...a: unknown[]) => rollbackRollout(...a),
}));

// RolloutsPage 用 useNamespace();受控 NamespaceContext 喂定全局 ns。默认「全部命名空间」(null)。
const renderPage = (namespace: SelectedNamespace | null = null): RenderResult =>
  render(
    <NamespaceContext.Provider
      value={{ namespace, setNamespace: () => {}, options: [], optionsLoading: false }}
    >
      <RolloutsPage />
    </NamespaceContext.Provider>,
  );

// jsdom 不实现以下 API,ProTable / antd Select/Drawer/Popconfirm 会调用,补最小 stub。
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

// 投放记录信封:对齐后端 RolloutOut(camelCase),逐字段对齐契约。
//  - 行 1(running):进行中、未冻结 → 无重试/回滚按钮、冻结列「-」。
//  - 行 2(failed + frozen + previousTarget 有):失败冻结 → 重试 + 回滚都显示。
//  - 行 3(failed 但 previousTarget 空):失败 → 只显重试,不显回滚(避免点了必 409)。
const rolloutsEnvelope = {
  count: 3,
  rows: [
    {
      id: 'ro-running',
      namespace: 'ns-admin',
      serviceName: 'wms-admin',
      mode: 'restart',
      trigger: 'manual',
      target: 'img:1.7.20',
      previousTarget: null,
      status: 'running',
      frozen: false,
      rollingTaskId: 'task-1',
      error: null,
      force: false,
      createdAt: '2026-06-20T10:00:00Z',
      finishedAt: null,
    },
    {
      id: 'ro-failed-frozen',
      namespace: 'ns-prod',
      serviceName: 'wms-scan',
      mode: 'restart',
      trigger: 'publish',
      target: 'img:1.7.21',
      previousTarget: 'img:1.7.20',
      status: 'failed',
      frozen: true,
      rollingTaskId: 'task-2',
      error: '节点 10.0.0.2:8080 失败,停止滚动',
      force: false,
      createdAt: '2026-06-19T10:00:00Z',
      finishedAt: '2026-06-19T10:05:00Z',
    },
    {
      id: 'ro-failed-noprev',
      namespace: 'ns-prod',
      serviceName: 'wms-erp',
      mode: 'restart',
      trigger: 'manual',
      target: 'img:1.7.21',
      previousTarget: null,
      status: 'failed',
      frozen: false,
      rollingTaskId: null,
      error: null,
      force: false,
      createdAt: '2026-06-18T10:00:00Z',
      finishedAt: '2026-06-18T10:05:00Z',
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

// 投放详情(行 2):含 rollingTask 逐实例进度(一个 done + 一个 failed)。
const detailFrozen = {
  ...rolloutsEnvelope.rows[1],
  rollingTask: {
    taskId: 'task-2',
    agentId: '*',
    serviceName: 'wms-scan',
    status: 'failed',
    degraded: false,
    nodes: [
      { address: '10.0.0.1:8080', containerId: 'c-1', status: 'done' },
      { address: '10.0.0.2:8080', containerId: 'c-2', status: 'failed', error: '重启超时' },
    ],
    error: '节点 10.0.0.2:8080 失败,停止滚动',
    createdAt: '2026-06-19T10:00:00Z',
    updatedAt: '2026-06-19T10:05:00Z',
    finishedAt: '2026-06-19T10:05:00Z',
  },
};

const emptyEnvelope = { count: 0, rows: [], page: 1, pageSize: 20, totalPage: 0 };

// 打开某个查询项 Select 并展开下拉(按 combobox id 定位,= dataIndex)。
const openSelect = async (user: ReturnType<typeof userEvent.setup>, fieldId: string) => {
  const combobox = await waitFor(() => {
    const el = document.getElementById(fieldId);
    if (!el) throw new Error(`combobox #${fieldId} 未渲染`);
    return el;
  });
  await user.click(combobox);
};

const clickOption = async (user: ReturnType<typeof userEvent.setup>, contains: string) => {
  const option = await screen.findByText(
    (_t, node) =>
      node?.classList.contains('ant-select-item-option-content') === true &&
      node.textContent?.includes(contains) === true,
  );
  await user.click(option);
};

// 点 Popconfirm 气泡里的「确认」按钮。行内触发按钮(<Button type="link">)与气泡确认按钮文案相同
// (都叫「重试」/「回滚」),按 role+name 会撞两个;故先等气泡(.ant-popover)出现,再在其内取确认按钮。
const confirmPopconfirm = async (user: ReturnType<typeof userEvent.setup>, okText: string) => {
  const popover = await waitFor(() => {
    const el = document.querySelector('.ant-popover:not(.ant-popover-hidden)');
    if (!el) throw new Error('Popconfirm 气泡未出现');
    return el as HTMLElement;
  });
  const btn = await within(popover).findByRole('button', { name: byNormalizedName(okText) });
  await user.click(btn);
};

describe('RolloutsPage(投放记录页)', () => {
  beforeEach(() => {
    listRollouts.mockReset();
    getRollout.mockReset();
    retryRollout.mockReset();
    rollbackRollout.mockReset();
    listRollouts.mockResolvedValue(rolloutsEnvelope);
    getRollout.mockResolvedValue(detailFrozen);
  });

  it('渲染投放行:服务/状态 Tag/冻结列(failed+frozen 显「冻结待人工」)+ 服务端分页', async () => {
    renderPage();

    // 服务列(蓝 tag)。
    expect(await screen.findByText('wms-admin')).toBeInTheDocument();
    expect(screen.getByText('wms-scan')).toBeInTheDocument();
    expect(screen.getByText('wms-erp')).toBeInTheDocument();

    // 状态 Tag:running→进行中、failed→失败(出现两行失败)。
    const runningRow = screen.getByText('wms-admin').closest('tr')!;
    expect(within(runningRow).getByText('进行中')).toBeInTheDocument();
    expect(screen.getAllByText('失败').length).toBeGreaterThanOrEqual(2);

    // 冻结列:行 2(failed+frozen)显「冻结待人工」;行 1/3 未冻结显「-」。
    const frozenRow = screen.getByText('wms-scan').closest('tr')!;
    expect(within(frozenRow).getByText('冻结待人工')).toBeInTheDocument();

    // 服务端分页:首屏即调一次 listRollouts,入参含 page/pageSize。
    await waitFor(() => expect(listRollouts).toHaveBeenCalled());
    const params = listRollouts.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(params.page).toBeDefined();
    expect(params.pageSize).toBeDefined();
  });

  it('重试按钮仅 failed 显示:running 行无「重试」,failed 行有「重试」', async () => {
    renderPage();
    await screen.findByText('wms-admin');

    // running 行(行 1):无重试、无回滚。
    const runningRow = screen.getByText('wms-admin').closest('tr')!;
    expect(within(runningRow).queryByText('重试')).not.toBeInTheDocument();
    expect(within(runningRow).queryByText('回滚')).not.toBeInTheDocument();

    // failed 行(行 2):有重试。
    const failedRow = screen.getByText('wms-scan').closest('tr')!;
    expect(within(failedRow).getByText('重试')).toBeInTheDocument();
  });

  it('回滚按钮仅 failed 且 previousTarget 非空 显示:有 prev 的失败行有「回滚」,无 prev 的失败行无「回滚」', async () => {
    renderPage();
    await screen.findByText('wms-scan');

    // 行 2(failed + previousTarget 有)→ 有回滚。
    const withPrev = screen.getByText('wms-scan').closest('tr')!;
    expect(within(withPrev).getByText('回滚')).toBeInTheDocument();

    // 行 3(failed 但 previousTarget 空)→ 有重试但无回滚(避免点了必 409)。
    const noPrev = screen.getByText('wms-erp').closest('tr')!;
    expect(within(noPrev).getByText('重试')).toBeInTheDocument();
    expect(within(noPrev).queryByText('回滚')).not.toBeInTheDocument();
  });

  it('详情 Drawer:点「详情」→ getRollout(id) → 展示字段 + rollingTask 逐实例进度(done/failed)', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-scan');

    // 点行 2 的「详情」。
    const failedRow = screen.getByText('wms-scan').closest('tr')!;
    await user.click(within(failedRow).getByText('详情'));

    // 关键:按该行 id 拉详情。
    await waitFor(() => expect(getRollout).toHaveBeenCalledWith('ro-failed-frozen'));

    // Drawer 顶 frozen 告警。
    expect(await screen.findByText('失败已冻结(半迁移态),可重试或回滚')).toBeInTheDocument();

    // 逐实例进度:两个实例 address + 状态(完成/失败)+ 失败实例 error。
    expect(await screen.findByText('10.0.0.1:8080')).toBeInTheDocument();
    expect(screen.getByText('10.0.0.2:8080')).toBeInTheDocument();
    expect(screen.getByText('完成')).toBeInTheDocument();
    // 逐实例状态「失败」与投放状态 Tag「失败」可能同名,断言至少出现一次。
    expect(screen.getAllByText('失败').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('重启超时')).toBeInTheDocument();
  });

  it('重试:failed 行「重试」→ Popconfirm 确认 → retryRollout(id) + 成功提示', async () => {
    retryRollout.mockResolvedValue({ rolloutId: 'ro-new', taskId: 'task-new' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-scan');

    const failedRow = screen.getByText('wms-scan').closest('tr')!;
    await user.click(within(failedRow).getByText('重试'));

    // Popconfirm 气泡确认按钮(okText=重试);点它触发 retryRollout。
    await confirmPopconfirm(user, '重试');

    await waitFor(() => expect(retryRollout).toHaveBeenCalledWith('ro-failed-frozen'));
    // 成功 toast 含新 rolloutId。
    expect(await screen.findByText((t) => t.includes('已发起重试') && t.includes('ro-new'))).toBeInTheDocument();
  });

  it('回滚:failed+prev 行「回滚」→ Popconfirm 确认 → rollbackRollout(id) + 成功提示', async () => {
    rollbackRollout.mockResolvedValue({ rolloutId: 'ro-rb', taskId: 'task-rb' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-scan');

    const withPrev = screen.getByText('wms-scan').closest('tr')!;
    await user.click(within(withPrev).getByText('回滚'));

    await confirmPopconfirm(user, '回滚');

    await waitFor(() => expect(rollbackRollout).toHaveBeenCalledWith('ro-failed-frozen'));
    expect(await screen.findByText((t) => t.includes('已发起回滚') && t.includes('ro-rb'))).toBeInTheDocument();
  });

  it('按服务名筛选:输入 serviceName → 查询 → listRollouts({serviceName}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-admin');

    const input = (await waitFor(() => {
      const el = document.getElementById('filterServiceName');
      if (!el) throw new Error('serviceName 输入框未渲染');
      return el;
    })) as HTMLInputElement;
    await user.type(input, 'wms-scan');
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    await waitFor(() =>
      expect(listRollouts).toHaveBeenCalledWith(expect.objectContaining({ serviceName: 'wms-scan' })),
    );
  });

  it('按状态筛选:选 failed → 查询 → listRollouts({status:"failed"}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-admin');

    await openSelect(user, 'filterStatus');
    await clickOption(user, '失败');
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    await waitFor(() =>
      expect(listRollouts).toHaveBeenCalledWith(expect.objectContaining({ status: 'failed' })),
    );
  });

  it('选了具体命名空间 → 强制以其 code 作 namespace 过滤(首屏即注入)', async () => {
    renderPage({ id: 2, code: 'ns-prod' });
    await waitFor(() =>
      expect(listRollouts).toHaveBeenCalledWith(expect.objectContaining({ namespace: 'ns-prod' })),
    );
  });

  it('空筛选值不透传:重置后再查询,listRollouts 入参不含 serviceName/status 空串', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-admin');

    const input = document.getElementById('filterServiceName') as HTMLInputElement;
    await user.type(input, 'x');
    await user.clear(input);
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    await waitFor(() => expect(listRollouts).toHaveBeenCalled());
    for (const call of listRollouts.mock.calls) {
      const p = (call[0] ?? {}) as Record<string, unknown>;
      expect(p.serviceName).not.toBe('');
      expect(p.status).not.toBe('');
    }
  });

  it('空态:后端回 0 行 → 不抛错,渲染空表', async () => {
    listRollouts.mockResolvedValue(emptyEnvelope);
    renderPage();
    await waitFor(() => expect(listRollouts).toHaveBeenCalled());
    expect(screen.queryByText('wms-admin')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText('暂无数据').length).toBeGreaterThanOrEqual(1));
  });

  it('详情加载失败 → 抽屉内错误 + 重试,点重试后渲染详情', async () => {
    let failDetail = true;
    getRollout.mockImplementation(() => {
      if (failDetail) {
        failDetail = false;
        return Promise.reject(new Error('detail-boom'));
      }
      return Promise.resolve(detailFrozen);
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-scan');

    const failedRow = screen.getByText('wms-scan').closest('tr')!;
    await user.click(within(failedRow).getByText('详情'));

    expect(await screen.findByText('加载投放详情失败')).toBeInTheDocument();
    const retries = await screen.findAllByRole('button', { name: byNormalizedName('重试') });
    await user.click(retries[retries.length - 1]);
    // 重试成功 → 逐实例进度出现。
    expect(await screen.findByText('10.0.0.1:8080')).toBeInTheDocument();
  });

  it('重试失败(409)→ 不抛错(由全局拦截器兜底),行仍在', async () => {
    retryRollout.mockRejectedValue({ response: { status: 409 } });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('wms-scan');

    const failedRow = screen.getByText('wms-scan').closest('tr')!;
    await user.click(within(failedRow).getByText('重试'));
    await confirmPopconfirm(user, '重试');

    await waitFor(() => expect(retryRollout).toHaveBeenCalledWith('ro-failed-frozen'));
    // 失败后该行仍在,不崩溃。
    expect(screen.getByText('wms-scan')).toBeInTheDocument();
  });
});
