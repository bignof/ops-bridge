import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ReleasesPage from '../ReleasesPage';

// mock 资源层:发布页所有数据访问都走 ../../api/resources。
//  - listReleases       → 主表(**不传 filter**,后端按 isActive=yes 回每绑定当前激活行)
//  - listReleaseHistory → 历史抽屉(**按 {serviceId, pluginId} 服务端过滤**取全部 spv)
//  - list               → 级联各级 options(namespaces / services?namespaceId / service-plugins?serviceId)
//  - listPluginVersions → 版本级联 options(?pluginId)
//  - publish/reactivate/rollback → 写动作,断言目标
const list = vi.fn();
const listReleases = vi.fn();
const listReleaseHistory = vi.fn();
const listPluginVersions = vi.fn();
const publish = vi.fn();
const reactivate = vi.fn();
const rollback = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
  listReleases: (...a: unknown[]) => listReleases(...a),
  listReleaseHistory: (...a: unknown[]) => listReleaseHistory(...a),
  listPluginVersions: (...a: unknown[]) => listPluginVersions(...a),
  publish: (...a: unknown[]) => publish(...a),
  reactivate: (...a: unknown[]) => reactivate(...a),
  rollback: (...a: unknown[]) => rollback(...a),
}));

// antd 在两个汉字按钮间插空格,带图标按钮的 accessible name 还含图标名;抹空白后用 includes 匹配。
const byNormalizedName = (text: string) => (name: string) => name.replace(/\s/g, '').includes(text);

// jsdom 不实现以下 API,antd Select 的虚拟列表/滚动会调用,补最小 stub 保证下拉可交互。
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

// 主表信封:一行「当前 active 绑定」(列用后端 JOIN 回的可读名;含 serviceId/pluginId 供行操作定位)。
const releasesEnvelope = {
  count: 1,
  rows: [
    {
      id: 100,
      namespaceCode: 'ns-demo',
      serviceName: 'svc-demo',
      serviceCode: 'svc-demo',
      pluginCode: 'plugin-demo',
      version: '1.2.0',
      publishTime: '2026-06-20 10:00:00',
      isActive: 'yes',
      isRolledBack: 'no',
      serviceId: 2,
      pluginId: 3,
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

// 历史抽屉信封:该绑定全部 spv 历史(含一行非 active,可「重新激活」/对其回滚)。
const historyEnvelope = {
  count: 2,
  rows: [
    {
      id: 100,
      version: '1.2.0',
      versionOrder: 2,
      publishTime: '2026-06-20 10:00:00',
      isActive: 'yes',
      isRolledBack: 'no',
      serviceId: 2,
      pluginId: 3,
    },
    {
      id: 99,
      version: '1.1.0',
      versionOrder: 1,
      publishTime: '2026-06-10 09:00:00',
      isActive: 'no',
      isRolledBack: 'no',
      serviceId: 2,
      pluginId: 3,
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

const namespacesEnvelope = {
  count: 1,
  rows: [{ id: 1, code: 'ns-demo', name: '演示命名空间' }],
  page: 1,
  pageSize: 100,
  totalPage: 1,
};
const servicesEnvelope = {
  count: 1,
  rows: [{ id: 2, serviceCode: 'svc-demo', name: '演示服务' }],
  page: 1,
  pageSize: 100,
  totalPage: 1,
};
// 服务已绑定插件(service-plugins?serviceId=):value 用绑定行回的 pluginId(=3)。
const servicePluginsEnvelope = {
  count: 1,
  rows: [{ id: 50, pluginId: 3, pluginCode: 'plugin-demo' }],
  page: 1,
  pageSize: 100,
  totalPage: 1,
};
const pluginVersionsEnvelope = {
  count: 1,
  rows: [{ id: 9, version: '1.2.0' }],
  page: 1,
  pageSize: 100,
  totalPage: 1,
};

// 按 resource 路由 list 返回(级联各级)。
const routeList = (resource: string) => {
  switch (resource) {
    case 'namespaces':
      return namespacesEnvelope;
    case 'services':
      return servicesEnvelope;
    case 'service-plugins':
      return servicePluginsEnvelope;
    default:
      return namespacesEnvelope;
  }
};

// 打开某个表单 Select 并展开下拉(按 form-item 的 combobox id = dataIndex 唯一锁定,避开表头同名列)。
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

describe('ReleasesPage', () => {
  beforeEach(() => {
    list.mockReset();
    listReleases.mockReset();
    listReleaseHistory.mockReset();
    listPluginVersions.mockReset();
    publish.mockReset();
    reactivate.mockReset();
    rollback.mockReset();
    list.mockImplementation((resource: string) => Promise.resolve(routeList(resource)));
    listReleases.mockResolvedValue(releasesEnvelope);
    listReleaseHistory.mockResolvedValue(historyEnvelope);
    listPluginVersions.mockResolvedValue(pluginVersionsEnvelope);
  });

  it('主表走 listReleases 且不带 filter(后端按 isActive=yes 聚合);active/回滚用 Tag 标色', async () => {
    render(<ReleasesPage />);

    // 列直接用后端 JOIN 回的可读名。
    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();
    expect(screen.getByText('ns-demo')).toBeInTheDocument();
    expect(screen.getByText('1.2.0')).toBeInTheDocument();

    // 关键断言:主表调用 listReleases,且参数**只含分页**、不含任何业务过滤键
    // (serviceId/pluginId/isActive 等都不应出现 —— 主表是聚合视图,不传 filter)。
    await waitFor(() => expect(listReleases).toHaveBeenCalled());
    const arg = (listReleases.mock.calls[0]?.[0] ?? {}) as Record<string, unknown>;
    expect(arg.serviceId).toBeUndefined();
    expect(arg.pluginId).toBeUndefined();
    expect(arg.isActive).toBeUndefined();
    expect(arg.filter).toBeUndefined();
    expect(Object.keys(arg).sort()).toEqual(['page', 'pageSize']);

    // active=yes → 运行中 Tag;isRolledBack=no → 不标「已回滚」。
    expect(screen.getByText('运行中')).toBeInTheDocument();
    expect(screen.queryByText('已回滚')).not.toBeInTheDocument();
  });

  it('点「发布」→ 四级级联逐级服务端过滤 → 提交 → 调 publish({serviceId,pluginId,pluginVersionId})', async () => {
    publish.mockResolvedValue({ id: 101 });
    const user = userEvent.setup();
    render(<ReleasesPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();

    // 开发布 Drawer(工具条「发布」)。
    await user.click(screen.getByRole('button', { name: byNormalizedName('发布') }));

    // 命名空间 options 来自服务端 list('namespaces')。
    await waitFor(() => expect(list).toHaveBeenCalledWith('namespaces', expect.anything()));
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');

    // 选命名空间后,服务下拉带 ?namespaceId= 服务端过滤(namespaceId=1)。
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith('services', expect.objectContaining({ namespaceId: 1 })),
    );
    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');

    // 选服务后,插件下拉取该服务已绑定插件 —— list('service-plugins', { serviceId })(serviceId=2)。
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith(
        'service-plugins',
        expect.objectContaining({ serviceId: 2 }),
      ),
    );
    await openSelect(user, 'pluginId');
    await clickOption(user, 'plugin-demo');

    // 选插件后,版本下拉带 ?pluginId= 服务端过滤(pluginId=3)。
    await waitFor(() =>
      expect(listPluginVersions).toHaveBeenCalledWith(expect.objectContaining({ pluginId: 3 })),
    );
    await openSelect(user, 'pluginVersionId');
    await clickOption(user, '1.2.0');

    // 提交 → publish({serviceId:2, pluginId:3, pluginVersionId:9})。
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));
    await waitFor(() =>
      expect(publish).toHaveBeenCalledWith({ serviceId: 2, pluginId: 3, pluginVersionId: 9 }),
    );
  });

  it('行「历史版本」→ 抽屉按 {serviceId,pluginId} 过滤 → 点回滚 → 调 rollback({spvId})', async () => {
    rollback.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<ReleasesPage />);

    const cell = await screen.findByText('plugin-demo');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('历史版本'));

    // 关键断言:历史抽屉调 listReleaseHistory,按 {serviceId:2, pluginId:3} 服务端过滤。
    await waitFor(() =>
      expect(listReleaseHistory).toHaveBeenCalledWith(
        expect.objectContaining({ serviceId: 2, pluginId: 3 }),
      ),
    );

    // 历史行渲染(1.1.0 为非 active 历史版本)。
    expect(await screen.findByText('1.1.0')).toBeInTheDocument();

    // 点主表行的「回滚」(Popconfirm 确认)→ rollback({spvId:100})。
    await user.click(within(row).getByText('回滚'));
    await user.click(await screen.findByRole('button', { name: byNormalizedName('回滚') }));
    await waitFor(() => expect(rollback).toHaveBeenCalledWith({ spvId: 100 }));
  });

  it('历史抽屉行「重新激活」非 active 版 → 调 reactivate({spvId})', async () => {
    reactivate.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<ReleasesPage />);

    const cell = await screen.findByText('plugin-demo');
    await user.click(within(cell.closest('tr')!).getByText('历史版本'));

    // 等历史行渲染,对非 active 版本(1.1.0,id=99)点「重新激活」。
    const histCell = await screen.findByText('1.1.0');
    const histRow = histCell.closest('tr')!;
    await user.click(within(histRow).getByText('重新激活'));

    await waitFor(() => expect(reactivate).toHaveBeenCalledWith({ spvId: 99 }));
  });
});
