import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ServicePluginsPage from '../ServicePluginsPage';

// mock 资源层:服务配置(二级页)所有数据访问都走 ../../api/resources。
//  - list('services')                         → 顶部「选服务」下拉
//  - list('service-plugins', {serviceId})     → 选定服务的绑定行(源真相,后端不回版本)
//  - listReleases({...})                      → releases 主表(join 出每绑定「当前版本」)
//  - list('plugins')                          → 「绑定插件」弹窗候选(剔除已绑)
//  - create('service-plugins', {...})         → 绑定
//  - remove('service-plugins', id)            → 解绑
//  - listReleaseHistory({serviceId,pluginId}) → 「改版本」抽屉的版本历史
//  - reactivate({spvId})                      → 改版本(重新激活历史版本)
const list = vi.fn();
const listReleases = vi.fn();
const listReleaseHistory = vi.fn();
const create = vi.fn();
const remove = vi.fn();
const reactivate = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
  listReleases: (...a: unknown[]) => listReleases(...a),
  listReleaseHistory: (...a: unknown[]) => listReleaseHistory(...a),
  create: (...a: unknown[]) => create(...a),
  remove: (...a: unknown[]) => remove(...a),
  reactivate: (...a: unknown[]) => reactivate(...a),
}));

// jsdom 不实现以下 API,antd Select/Drawer/Modal(虚拟列表/portal/滚动锁)会调用,补最小 stub 保证可交互。
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

// 信封工厂:统一 {count, rows, page, pageSize, totalPage}。
const envelope = <T,>(rows: T[]) => ({
  count: rows.length,
  rows,
  page: 1,
  pageSize: 200,
  totalPage: 1,
});

// 服务列表(顶部选服务):svc-demo(id=2) / svc-other(id=20)。
const servicesEnvelope = envelope([
  { id: 2, serviceCode: 'svc-demo', name: '演示服务' },
  { id: 20, serviceCode: 'svc-other', name: '另一服务' },
]);

// svc-demo(id=2)的绑定:plugin-a(pluginId=3,已发布)、plugin-b(pluginId=4,未发布)。
const bindingsEnvelope = envelope([
  { id: 11, serviceId: 2, pluginId: 3, pluginCode: 'plugin-a' },
  { id: 12, serviceId: 2, pluginId: 4, pluginCode: 'plugin-b' },
]);

// releases 主表:仅 plugin-a(serviceId=2,pluginId=3)有 active 版本 1.2.0;plugin-b 无 → 「未发布」。
const releasesEnvelope = envelope([
  {
    id: 101,
    serviceId: 2,
    pluginId: 3,
    version: '1.2.0',
    versionOrder: 2,
    isActive: true,
    isRolledBack: false,
    publishTime: '2026-06-01T10:00:00Z',
  },
]);

// 全量插件(「绑定插件」弹窗候选):plugin-a(3,已绑)、plugin-b(4,已绑)、plugin-c(5,可绑)。
const pluginsEnvelope = envelope([
  { id: 3, code: 'plugin-a' },
  { id: 4, code: 'plugin-b' },
  { id: 5, code: 'plugin-c' },
]);

// plugin-a 的版本历史:1.1.0(历史)、1.2.0(运行中)。改版本=把 1.1.0 重新激活。
const historyEnvelope = envelope([
  {
    id: 100,
    serviceId: 2,
    pluginId: 3,
    version: '1.1.0',
    versionOrder: 1,
    isActive: false,
    isRolledBack: false,
    publishTime: '2026-05-01T10:00:00Z',
  },
  {
    id: 101,
    serviceId: 2,
    pluginId: 3,
    version: '1.2.0',
    versionOrder: 2,
    isActive: true,
    isRolledBack: false,
    publishTime: '2026-06-01T10:00:00Z',
  },
]);

// 按 resource 路由 list 返回。
const routeList = (resource: string) => {
  switch (resource) {
    case 'services':
      return servicesEnvelope;
    case 'service-plugins':
      return bindingsEnvelope;
    case 'plugins':
      return pluginsEnvelope;
    default:
      return envelope([]);
  }
};

// 打开顶部服务下拉并选中 svc-demo(下拉项渲染在 portal 的 .ant-select-item-option-content)。
const selectService = async (user: ReturnType<typeof userEvent.setup>, contains: string) => {
  // 顶部只有一个 combobox(选服务);用 role 定位最稳。
  const combobox = await screen.findByRole('combobox');
  await user.click(combobox);
  const option = await screen.findByText(
    (_t, node) =>
      node?.classList.contains('ant-select-item-option-content') === true &&
      node.textContent?.includes(contains) === true,
  );
  await user.click(option);
};

// 点选某个下拉项(portal 内 option content 文案匹配)。
const clickOption = async (user: ReturnType<typeof userEvent.setup>, contains: string) => {
  const option = await screen.findByText(
    (_t, node) =>
      node?.classList.contains('ant-select-item-option-content') === true &&
      node.textContent?.includes(contains) === true,
  );
  await user.click(option);
};

describe('ServicePluginsPage(服务配置二级页)', () => {
  beforeEach(() => {
    list.mockReset();
    listReleases.mockReset();
    listReleaseHistory.mockReset();
    create.mockReset();
    remove.mockReset();
    reactivate.mockReset();
    list.mockImplementation((resource: string) => Promise.resolve(routeList(resource)));
    listReleases.mockResolvedValue(releasesEnvelope);
    listReleaseHistory.mockResolvedValue(historyEnvelope);
  });

  it('初始:加载服务列表,未选服务时显占位引导(不拉绑定)', async () => {
    render(<ServicePluginsPage />);
    expect(await screen.findByText('请选择一个服务以配置其插件')).toBeInTheDocument();
    // 顶部服务下拉拉过 services。
    await waitFor(() => expect(list).toHaveBeenCalledWith('services', expect.anything()));
    // 未选服务:不应调 service-plugins。
    expect(list).not.toHaveBeenCalledWith('service-plugins', expect.anything());
  });

  it('选服务 → 拉该服务绑定(service-plugins?serviceId=)并渲染已绑插件 + join 当前版本(未发布兜底)', async () => {
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');

    await selectService(user, 'svc-demo');

    // 关键:选服务后按 serviceId 服务端过滤拉绑定。
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith(
        'service-plugins',
        expect.objectContaining({ serviceId: 2 }),
      ),
    );
    // 同时拉 releases 主表 join 版本。
    await waitFor(() => expect(listReleases).toHaveBeenCalled());

    // 两个已绑插件都渲染。
    expect(await screen.findByText('plugin-a')).toBeInTheDocument();
    expect(screen.getByText('plugin-b')).toBeInTheDocument();
    // plugin-a 有 active 版本 1.2.0;plugin-b 无 → 「未发布」。
    expect(screen.getByText('1.2.0')).toBeInTheDocument();
    expect(screen.getByText('未发布')).toBeInTheDocument();
  });

  it('绑定插件:打开弹窗(候选剔除已绑)→ 选 plugin-c → 提交调 create(service-plugins,{serviceId,pluginId})', async () => {
    create.mockResolvedValue({ id: 13 });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');
    await screen.findByText('plugin-a');

    // 打开「绑定插件」弹窗。
    await user.click(screen.getByRole('button', { name: byNormalizedName('绑定插件') }));
    await waitFor(() => expect(list).toHaveBeenCalledWith('plugins', expect.anything()));

    // 弹窗候选应剔除已绑(plugin-a/plugin-b),只剩 plugin-c —— 选它。
    const dialog = await screen.findByRole('dialog');
    const combobox = within(dialog).getByRole('combobox');
    await user.click(combobox);
    await clickOption(user, 'plugin-c');

    // 点弹窗「绑定」确认。
    await user.click(within(dialog).getByRole('button', { name: byNormalizedName('绑定') }));

    // 关键断言:create('service-plugins', { serviceId: 2, pluginId: 5 })。
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(
        'service-plugins',
        expect.objectContaining({ serviceId: 2, pluginId: 5 }),
      ),
    );
  });

  it('解绑:点「解绑」→ Popconfirm 确认 → remove(service-plugins, id)', async () => {
    remove.mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    // 定位 plugin-a 所在行的「解绑」。
    const cell = await screen.findByText('plugin-a');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('解绑'));

    // Popconfirm 气泡的确认按钮(okText=解绑);点它触发 remove。
    const confirmBtn = await screen.findByRole('button', { name: byNormalizedName('解绑') });
    await user.click(confirmBtn);

    await waitFor(() => expect(remove).toHaveBeenCalledWith('service-plugins', 11));
  });

  it('改版本:打开版本抽屉 → listReleaseHistory → 非 active 行「切到此版本」调 reactivate({spvId})', async () => {
    reactivate.mockResolvedValue({ id: 100 });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    // 打开 plugin-a 的「版本历史 / 改版本」抽屉。
    const cell = await screen.findByText('plugin-a');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('版本历史 / 改版本'));

    // 关键:抽屉按 serviceId+pluginId 拉历史。
    await waitFor(() =>
      expect(listReleaseHistory).toHaveBeenCalledWith(
        expect.objectContaining({ serviceId: 2, pluginId: 3 }),
      ),
    );

    // 历史出现两版;1.1.0 是非 active 行,有「切到此版本」按钮;1.2.0 是当前。
    expect(await screen.findByText('1.1.0')).toBeInTheDocument();
    const switchBtn = await screen.findByRole('button', { name: byNormalizedName('切到此版本') });
    await user.click(switchBtn);

    // 关键断言:reactivate({ spvId: 100 })(1.1.0 那行 id)。
    await waitFor(() => expect(reactivate).toHaveBeenCalledWith({ spvId: 100 }));
  });

  it('重复绑定 409 → 提示「该插件已绑定该服务,请勿重复关联」', async () => {
    create.mockRejectedValue({ response: { status: 409 } });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');
    await screen.findByText('plugin-a');

    await user.click(screen.getByRole('button', { name: byNormalizedName('绑定插件') }));
    const dialog = await screen.findByRole('dialog');
    const combobox = within(dialog).getByRole('combobox');
    await user.click(combobox);
    await clickOption(user, 'plugin-c');
    await user.click(within(dialog).getByRole('button', { name: byNormalizedName('绑定') }));

    expect(
      await screen.findByText((t) => t.includes('该插件已绑定该服务')),
    ).toBeInTheDocument();
  });

  it('空态:服务无绑定 → 显「该服务暂未绑定插件」', async () => {
    list.mockImplementation((resource: string) =>
      Promise.resolve(resource === 'service-plugins' ? envelope([]) : routeList(resource)),
    );
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    expect(
      await screen.findByText((t) => t.includes('该服务暂未绑定插件')),
    ).toBeInTheDocument();
  });

  it('错误态:拉绑定失败 → 显错误 + 重试按钮,点重试重新拉取', async () => {
    let failNext = true;
    list.mockImplementation((resource: string) => {
      if (resource === 'service-plugins') {
        if (failNext) {
          failNext = false;
          return Promise.reject(new Error('boom'));
        }
        return Promise.resolve(bindingsEnvelope);
      }
      return Promise.resolve(routeList(resource));
    });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    // 首次失败:错误提示出现。
    expect(await screen.findByText('加载该服务的插件绑定失败')).toBeInTheDocument();
    // 点重试 → 第二次成功 → 渲染绑定。
    await user.click(screen.getByRole('button', { name: byNormalizedName('重试') }));
    expect(await screen.findByText('plugin-a')).toBeInTheDocument();
  });

  it('服务列表加载失败 → 页面级错误 + 重试,点重试后渲染下拉占位', async () => {
    let failServices = true;
    list.mockImplementation((resource: string) => {
      if (resource === 'services') {
        if (failServices) {
          failServices = false;
          return Promise.reject(new Error('svc-boom'));
        }
        return Promise.resolve(servicesEnvelope);
      }
      return Promise.resolve(routeList(resource));
    });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);

    expect(await screen.findByText('加载服务列表失败')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('重试') }));
    // 第二次成功 → 回到「请选择一个服务」占位。
    expect(await screen.findByText('请选择一个服务以配置其插件')).toBeInTheDocument();
  });

  it('版本抽屉:该插件无已发布版本(历史空)→ 显「暂无已发布版本」引导', async () => {
    listReleaseHistory.mockResolvedValue(envelope([]));
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    // plugin-b 未发布;打开其版本抽屉。
    const cell = await screen.findByText('plugin-b');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('版本历史 / 改版本'));

    expect(
      await screen.findByText((t) => t.includes('该插件暂无已发布版本')),
    ).toBeInTheDocument();
  });

  it('版本抽屉:历史加载失败 → 抽屉内错误 + 重试,点重试后渲染历史', async () => {
    let failHistory = true;
    listReleaseHistory.mockImplementation(() => {
      if (failHistory) {
        failHistory = false;
        return Promise.reject(new Error('hist-boom'));
      }
      return Promise.resolve(historyEnvelope);
    });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    const cell = await screen.findByText('plugin-a');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('版本历史 / 改版本'));

    // 抽屉内错误态 + 重试。
    expect(await screen.findByText('加载版本历史失败')).toBeInTheDocument();
    // 抽屉内的「重试」(与页面无其它重试按钮共存,取最后一个=抽屉内)。
    const retries = await screen.findAllByRole('button', { name: byNormalizedName('重试') });
    await user.click(retries[retries.length - 1]);
    expect(await screen.findByText('1.1.0')).toBeInTheDocument();
  });

  it('改版本失败 → 不抛错(由全局拦截器兜底),按钮恢复可点', async () => {
    reactivate.mockRejectedValue({ response: { status: 409 } });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    const cell = await screen.findByText('plugin-a');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('版本历史 / 改版本'));
    const switchBtn = await screen.findByRole('button', { name: byNormalizedName('切到此版本') });
    await user.click(switchBtn);

    await waitFor(() => expect(reactivate).toHaveBeenCalledWith({ spvId: 100 }));
    // 失败后按钮仍在(loading 复位),不崩溃。
    expect(
      await screen.findByRole('button', { name: byNormalizedName('切到此版本') }),
    ).toBeInTheDocument();
  });

  it('解绑失败 → 不抛错(由全局拦截器兜底),行仍在', async () => {
    remove.mockRejectedValue(new Error('del-boom'));
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');

    const cell = await screen.findByText('plugin-a');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('解绑'));
    const confirmBtn = await screen.findByRole('button', { name: byNormalizedName('解绑') });
    await user.click(confirmBtn);

    await waitFor(() => expect(remove).toHaveBeenCalledWith('service-plugins', 11));
    // 失败后该行仍在(未误删 UI)。
    expect(screen.getByText('plugin-a')).toBeInTheDocument();
  });

  it('绑定弹窗:全部插件已绑 → 候选为空,notFoundContent 提示「已绑定全部可用插件」', async () => {
    // 插件全集 = 已绑的两个 → 候选剔空。
    list.mockImplementation((resource: string) =>
      Promise.resolve(
        resource === 'plugins'
          ? envelope([
              { id: 3, code: 'plugin-a' },
              { id: 4, code: 'plugin-b' },
            ])
          : routeList(resource),
      ),
    );
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');
    await screen.findByText('plugin-a');

    await user.click(screen.getByRole('button', { name: byNormalizedName('绑定插件') }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('combobox'));
    expect(
      await screen.findByText((t) => t.includes('该服务已绑定全部可用插件')),
    ).toBeInTheDocument();
  });

  it('B4 契约:所有 list / releases 调用 pageSize ≤ 200(后端硬卡,>200 会 422)', async () => {
    const user = userEvent.setup();
    render(<ServicePluginsPage />);
    await screen.findByText('请选择一个服务以配置其插件');
    await selectService(user, 'svc-demo');
    await screen.findByText('plugin-a');
    await user.click(screen.getByRole('button', { name: byNormalizedName('绑定插件') }));
    await waitFor(() => expect(list).toHaveBeenCalledWith('plugins', expect.anything()));

    for (const call of [...list.mock.calls, ...listReleases.mock.calls, ...listReleaseHistory.mock.calls]) {
      const ps = (call[call.length - 1] as { pageSize?: number } | undefined)?.pageSize;
      if (ps !== undefined) expect(ps).toBeLessThanOrEqual(200);
    }
  });
});
