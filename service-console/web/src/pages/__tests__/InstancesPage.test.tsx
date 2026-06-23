import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import InstancesPage from '../InstancesPage';
import { NamespaceContext, type SelectedNamespace } from '../../context/NamespaceContext';

// mock 资源层:实例页只读,数据访问只走 ../../api/resources.listInstances
//   listInstances(params) → 实例列表(服务端分页,信封 {count, rows, …};row = 后端 DiscoveredNodeOut camelCase)
// 捕获其入参以断言:服务端分页(page/pageSize)、namespace/status 过滤透传、空值不透传。
const listInstances = vi.fn();
vi.mock('../../api/resources', () => ({
  listInstances: (...a: unknown[]) => listInstances(...a),
}));

// 实例页用 useNamespace(),用受控 NamespaceContext 喂定全局 ns。默认「全部命名空间」(null);
// 传 namespace 模拟「选了某具体 ns」以验证强制过滤。
const renderPage = (namespace: SelectedNamespace | null = null): RenderResult =>
  render(
    <NamespaceContext.Provider
      value={{ namespace, setNamespace: () => {}, options: [], optionsLoading: false }}
    >
      <InstancesPage />
    </NamespaceContext.Provider>,
  );

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

// 日志抽屉打开即 fetch console SSE;本页用例 stub 一个返回单个 started 帧的流,验证「点日志→开抽屉→发起请求」。
// (SSE 解析/事件/abort 的细粒度断言在 InstanceLogDrawer.test.tsx,这里只验页面联动。)
const sseStartedStream = (): ReadableStream<Uint8Array> => {
  const enc = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(enc.encode('event: started\ndata: {"sessionId":"s1"}\n\n'));
      controller.close();
    },
  });
};

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
    renderPage();

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
    renderPage();
    await screen.findByText('ns-online');

    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    const params = listInstances.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(params.page).toBeDefined();
    expect(params.pageSize).toBeDefined();
  });

  it('行操作仅「日志」:无「添加/编辑/删除」,无启动/停止/更新(本期未接)', async () => {
    renderPage();
    await screen.findByText('ns-online');

    // 不渲染添加 / 编辑 / 删除。
    expect(screen.queryByText('添加')).not.toBeInTheDocument();
    expect(screen.queryByText('编辑')).not.toBeInTheDocument();
    expect(screen.queryByText('删除')).not.toBeInTheDocument();
    // 行内运维动作(启动/停止/更新)本期仍不接。
    const onlineRow = screen.getByText('ns-online').closest('tr')!;
    expect(within(onlineRow).queryByText('启动')).not.toBeInTheDocument();
    expect(within(onlineRow).queryByText('停止')).not.toBeInTheDocument();
    expect(within(onlineRow).queryByText('更新')).not.toBeInTheDocument();
  });

  it('按命名空间筛选:输入 namespace → 查询 → listInstances({namespace}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    renderPage();
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
    renderPage();
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
    renderPage();
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
    renderPage();

    // 列头仍在(表渲染成功),但没有任何实例数据行。
    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    expect(screen.queryByText('ns-online')).not.toBeInTheDocument();
    // antd 空态占位文案(ProTable 在表体与查询表单各渲一处 Empty,故用 getAllByText 取至少一处)。
    await waitFor(() => expect(screen.getAllByText('暂无数据').length).toBeGreaterThanOrEqual(1));
  });

  it('错误态:请求失败 → 不崩(无数据行),仍调用了 listInstances(全局兜底 toast 由 client 拦截器处理)', async () => {
    listInstances.mockRejectedValue({ response: { status: 500 } });
    renderPage();

    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    // 失败时不渲染数据行;页面不抛(ProTable request 失败走 success:false)。
    expect(screen.queryByText('ns-online')).not.toBeInTheDocument();
  });

  // ── 行操作「日志」(P3-9):有 dir 可点 → 开抽屉发起 SSE;无 dir 禁用 ─────────────────
  it('有 dir 的行「日志」按钮可点;点击打开日志抽屉并发起 SSE(POST 到该行 agentId)', async () => {
    // 抽屉打开即 fetch console SSE;stub 返回单 started 帧的流,捕获入参验证按行 agentId 寻址。
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, body: sseStartedStream() }) as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    sessionStorage.setItem('platform_token', 'tok-x');
    try {
      const user = userEvent.setup();
      renderPage();
      await screen.findByText('ns-online');

      // 行 1(ns-online)有 dir → 「日志」按钮可点。
      const onlineRow = screen.getByText('ns-online').closest('tr')!;
      const logBtn = within(onlineRow).getByRole('button', { name: byNormalizedName('日志') });
      expect(logBtn).toBeEnabled();

      await user.click(logBtn);

      // 抽屉打开:标题副标识 = 容器名;且对该行 agentId 发起 SSE。
      expect(await screen.findByText('· container-admin')).toBeInTheDocument();
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toBe('/api/agents/ns-online/logs/stream');
      const body = JSON.parse(init.body as string);
      expect(body.dir).toBe('/data/orchidea/admin');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('无 dir 的行「日志」按钮禁用(发现未取到工程目录)', async () => {
    // 行 2(ns-prod)的 dir 在 fixture 里有值;此处单独给一份 dir=null 的信封验证禁用态。
    listInstances.mockResolvedValue({
      ...instancesEnvelope,
      rows: [{ ...instancesEnvelope.rows[1], dir: null }],
      count: 1,
    });
    renderPage();
    const row = (await screen.findByText('ns-prod')).closest('tr')!;
    const logBtn = within(row).getByRole('button', { name: byNormalizedName('日志') });
    // 无 dir → 禁用(tooltip 说明原因)。
    expect(logBtn).toBeDisabled();
  });

  // ── P3-10:全局命名空间联动 ───────────────────────────────────────────────────
  it('选了具体命名空间 → 强制以其 code 作 namespace 过滤(覆盖本页筛选列,以全局为准)', async () => {
    // 受控全局 ns = {id:2, code:'ns-prod'};实例页按 code(=agentId)过滤。
    renderPage({ id: 2, code: 'ns-prod' });
    await waitFor(() => expect(listInstances).toHaveBeenCalled());

    // 关键:首屏 request 即带 namespace='ns-prod'(全局强制注入)。
    await waitFor(() =>
      expect(listInstances).toHaveBeenCalledWith(expect.objectContaining({ namespace: 'ns-prod' })),
    );
  });

  it('「全部命名空间」+ 切「按服务聚合」→ 跨机同名服务聚合(实例数/机器数/健康数)', async () => {
    // 聚合 fixture:wms-admin 跨双机(2 实例,1 健康)、wms-scan 单机(1 实例,1 健康)、一个无 nacos 匹配。
    listInstances.mockResolvedValue({
      count: 4,
      rows: [
        { ...instancesEnvelope.rows[0], agentId: 'ns-a', containerName: 'c1', nacosService: 'wms-admin', healthy: true },
        { ...instancesEnvelope.rows[0], agentId: 'ns-b', containerName: 'c2', nacosService: 'wms-admin', healthy: false },
        { ...instancesEnvelope.rows[0], agentId: 'ns-a', containerName: 'c3', nacosService: 'wms-scan', healthy: true },
        { ...instancesEnvelope.rows[1], agentId: 'ns-a', containerName: 'c4', nacosService: null, healthy: null },
      ],
      page: 1,
      pageSize: 200,
      totalPage: 1,
    });
    const user = userEvent.setup();
    renderPage(null); // 全部命名空间

    // 默认实例明细视图(等首屏列表出来,用唯一容器名 c1 确认渲染,避免 ns-a 多行歧义)。
    await screen.findByText('c1');

    // 切到「按服务聚合」。
    await user.click(screen.getByText('按服务聚合'));

    // 聚合表出现 wms-admin 行,跨双机 2 实例。用所在行断言「实例数/机器数/健康数」。
    const adminCell = await screen.findByText('wms-admin');
    const adminRow = adminCell.closest('tr')!;
    // 实例数=2、机器数=2(ns-a + ns-b)→ 该行出现两处「2」;健康数渲染为「1/2」(唯一,可精确断言)。
    expect(within(adminRow).getAllByText('2').length).toBe(2);
    expect(within(adminRow).getByText('1/2')).toBeInTheDocument();

    // 同时存在 wms-scan 行(单机 1 实例,健康 1/1)与未匹配 nacos 占位行。
    expect(screen.getByText('wms-scan')).toBeInTheDocument();
    expect(screen.getByText('(未匹配 nacos)')).toBeInTheDocument();

    // 聚合视图请求一次性拉 ≤200。
    await waitFor(() => {
      const aggCall = listInstances.mock.calls.find(
        (c) => (c[0] as { pageSize?: number })?.pageSize === 200,
      );
      expect(aggCall).toBeTruthy();
    });
  });

  it('选了具体命名空间时不显「按服务聚合」切换器(聚合是「全部」总览语义)', async () => {
    renderPage({ id: 2, code: 'ns-prod' });
    await waitFor(() => expect(listInstances).toHaveBeenCalled());
    // 下钻具体 ns 时不提供聚合视图切换。
    expect(screen.queryByText('按服务聚合')).not.toBeInTheDocument();
  });
});
