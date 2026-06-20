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
// isActive/isRolledBack 用 boolean(对齐 P1a ReleaseOut 契约:is_active/is_rolled_back 为 bool → JSON true/false)。
// 对齐真实 ReleaseOut 形状:**无 serviceName**(后端只回 serviceCode);publishTime 为 ISO8601(带 T/Z,后端 datetime 序列化)。
const releasesEnvelope = {
  count: 1,
  rows: [
    {
      id: 100,
      namespaceCode: 'ns-demo',
      serviceCode: 'svc-demo',
      pluginCode: 'plugin-demo',
      version: '1.2.0',
      publishTime: '2026-06-20T10:00:00Z',
      isActive: true,
      isRolledBack: false,
      serviceId: 2,
      pluginId: 3,
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

// 历史抽屉信封:该绑定全部 spv 历史(boolean 契约):
//  - id=100 当前 active(isActive=true) → 不应出现「重新激活」入口;
//  - id=99 历史非 active(isActive=false)且 isRolledBack=true → 出现「重新激活」且标「已回滚」。
const historyEnvelope = {
  count: 2,
  rows: [
    {
      id: 100,
      version: '1.2.0',
      versionOrder: 2,
      publishTime: '2026-06-20T10:00:00Z',
      isActive: true,
      isRolledBack: false,
      serviceId: 2,
      pluginId: 3,
    },
    {
      id: 99,
      version: '1.1.0',
      versionOrder: 1,
      publishTime: '2026-06-10T09:00:00Z',
      isActive: false,
      isRolledBack: true,
      serviceId: 2,
      pluginId: 3,
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

// 两个命名空间:A1 级联清空用例需切换父级,验证下级被清(单命名空间无法观测切换)。
const namespacesEnvelope = {
  count: 2,
  rows: [
    { id: 1, code: 'ns-demo', name: '演示命名空间' },
    { id: 10, code: 'ns-other', name: '另一命名空间' },
  ],
  page: 1,
  pageSize: 100,
  totalPage: 1,
};
// 服务按命名空间隔离:ns-demo(id=1)→ svc-demo(id=2);ns-other(id=10)→ svc-other(id=20)。
const servicesByNamespace: Record<number, unknown> = {
  1: {
    count: 1,
    rows: [{ id: 2, serviceCode: 'svc-demo', name: '演示服务' }],
    page: 1,
    pageSize: 100,
    totalPage: 1,
  },
  10: {
    count: 1,
    rows: [{ id: 20, serviceCode: 'svc-other', name: '另一服务' }],
    page: 1,
    pageSize: 100,
    totalPage: 1,
  },
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

// 按 resource 路由 list 返回(级联各级);services 再按 namespaceId 服务端过滤分流。
const routeList = (resource: string, params?: Record<string, unknown>) => {
  switch (resource) {
    case 'namespaces':
      return namespacesEnvelope;
    case 'services':
      return (
        servicesByNamespace[Number(params?.namespaceId)] ?? {
          count: 0,
          rows: [],
          page: 1,
          pageSize: 100,
          totalPage: 1,
        }
      );
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
    list.mockImplementation((resource: string, params?: Record<string, unknown>) =>
      Promise.resolve(routeList(resource, params)),
    );
    listReleases.mockResolvedValue(releasesEnvelope);
    listReleaseHistory.mockResolvedValue(historyEnvelope);
    listPluginVersions.mockResolvedValue(pluginVersionsEnvelope);
  });

  it('主表走 listReleases 且不带 filter(后端按 isActive 聚合);active/回滚用 boolean → Tag 标色', async () => {
    render(<ReleasesPage />);

    // 列直接用后端 JOIN 回的可读名。
    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();
    expect(screen.getByText('ns-demo')).toBeInTheDocument();
    expect(screen.getByText('1.2.0')).toBeInTheDocument();

    // C1:发布时间经 valueType 'dateTime' 格式化(本地时区),不直显原始 ISO(带 T/Z)。
    expect(screen.queryByText('2026-06-20T10:00:00Z')).not.toBeInTheDocument();
    expect(
      screen.getByText((t) => /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(t)),
    ).toBeInTheDocument();

    // 关键断言:主表调用 listReleases,且参数**只含分页**、不含任何业务过滤键
    // (serviceId/pluginId/isActive 等都不应出现 —— 主表是聚合视图,不传 filter)。
    await waitFor(() => expect(listReleases).toHaveBeenCalled());
    const arg = (listReleases.mock.calls[0]?.[0] ?? {}) as Record<string, unknown>;
    expect(arg.serviceId).toBeUndefined();
    expect(arg.pluginId).toBeUndefined();
    expect(arg.isActive).toBeUndefined();
    expect(arg.filter).toBeUndefined();
    expect(Object.keys(arg).sort()).toEqual(['page', 'pageSize']);

    // C1 守卫:isActive=true(boolean)→ 运行中 Tag,绝不显「历史」;isRolledBack=false → 不标「已回滚」。
    // (旧实现按 'yes' 判,boolean true 恒不命中 → 误显「历史」/不显「运行中」;此断言抓该漂移。)
    expect(screen.getByText('运行中')).toBeInTheDocument();
    expect(screen.queryByText('历史')).not.toBeInTheDocument();
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

    // B4 契约钉死:后端各 list 端点硬卡 pageSize le=200,前端级联下拉一切取值 **必须 ≤ 200**,
    // 否则真后端 422 下拉崩(G3 曾抬到 500/100 是真回归)。逐一断言每次 list / listPluginVersions
    // 的 pageSize 都不超过 200。
    for (const call of list.mock.calls) {
      const ps = (call[1] as { pageSize?: number } | undefined)?.pageSize;
      if (ps !== undefined) expect(ps).toBeLessThanOrEqual(200);
    }
    for (const call of listPluginVersions.mock.calls) {
      const ps = (call[0] as { pageSize?: number } | undefined)?.pageSize;
      if (ps !== undefined) expect(ps).toBeLessThanOrEqual(200);
    }
  });

  it('A1 级联清空:发布抽屉选命名空间 A→服务→插件→版本后,改命名空间 B → 下级(服务/插件/版本)全被清空', async () => {
    const user = userEvent.setup();
    render(<ReleasesPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('发布') }));

    // 选命名空间 A(ns-demo,id=1)→ 选其服务 svc-demo(id=2)→ 选插件 → 选版本,逐级选满。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');
    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');
    await openSelect(user, 'pluginId');
    await clickOption(user, 'plugin-demo');
    await openSelect(user, 'pluginVersionId');
    await clickOption(user, '1.2.0');

    // 选满后,各级渲染出已选项文案。
    const serviceField = document.getElementById('serviceId')!.closest('.ant-select')!;
    const pluginField = document.getElementById('pluginId')!.closest('.ant-select')!;
    const versionField = document.getElementById('pluginVersionId')!.closest('.ant-select')!;
    await waitFor(() =>
      expect(serviceField.querySelector('.ant-select-selection-item')?.textContent).toContain(
        'svc-demo',
      ),
    );
    expect(pluginField.querySelector('.ant-select-selection-item')?.textContent).toContain(
      'plugin-demo',
    );
    expect(versionField.querySelector('.ant-select-selection-item')?.textContent).toContain('1.2.0');

    // 改命名空间为 B(ns-other,id=10)。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-other');

    // A1 关键断言:换命名空间后,服务/插件/版本三级已选值全部被清空(不残留 A 的旧值 → 杜绝错配提交)。
    await waitFor(() =>
      expect(serviceField.querySelector('.ant-select-selection-item')?.textContent ?? '').not.toContain(
        'svc-demo',
      ),
    );
    expect(pluginField.querySelector('.ant-select-selection-item')?.textContent ?? '').not.toContain(
      'plugin-demo',
    );
    expect(versionField.querySelector('.ant-select-selection-item')?.textContent ?? '').not.toContain(
      '1.2.0',
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

  it('历史抽屉:active 行不露「重新激活」(显「当前运行」)、非 active 行可「重新激活」→ 调 reactivate({spvId})、回滚行标「已回滚」', async () => {
    reactivate.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<ReleasesPage />);

    const cell = await screen.findByText('plugin-demo');
    await user.click(within(cell.closest('tr')!).getByText('历史版本'));

    // 等历史行渲染(1.1.0=非 active 历史版本,id=99;1.2.0=当前 active,id=100)。
    // 注:版本号 1.2.0 在主表「当前版本」列也出现,故把后续行查询**限定在抽屉表格内**(histTable),避免误中主表行。
    const histCell = await screen.findByText('1.1.0');
    const histRow = histCell.closest('tr')!;
    const histTable = histCell.closest('table')!;

    // C1 守卫①:当前 active 行(1.2.0,isActive=true)**不应**出现可点的「重新激活」,而显「当前运行」。
    // (旧实现 === 'yes' 守卫对 boolean true 永不命中 → active 行错误露出「重新激活」;此断言抓该漂移。)
    const activeRow = within(histTable).getByText('1.2.0').closest('tr')!;
    expect(within(activeRow).queryByText('重新激活')).not.toBeInTheDocument();
    expect(within(activeRow).getByText('当前运行')).toBeInTheDocument();

    // C1 守卫②:非 active 行(isActive=false)才出现「重新激活」,点击 → reactivate({spvId:99})。
    await user.click(within(histRow).getByText('重新激活'));
    await waitFor(() => expect(reactivate).toHaveBeenCalledWith({ spvId: 99 }));

    // C1 守卫③:isRolledBack=true(boolean)→ 该非 active 行标「已回滚」。
    expect(within(histRow).getByText('已回滚')).toBeInTheDocument();
  });

  // Minor-6(A2 兜底分支):publish 在资源层 opt-out 全局兜底,故 handlePublish 必须自管非 409 失败的
  // 可见性(else if status!==401 通用兜底 toast),且失败时**不关抽屉**(返回 false)便于用户重试。
  // 后端专设 502(hub 不可用)→ 须见「发布失败」类提示,抽屉保持打开。
  it('发布 502(hub 不可用)→ 通用兜底「发布失败」提示且抽屉不关闭(A2 写失败不静默吞)', async () => {
    publish.mockRejectedValue({ response: { status: 502 } });
    const user = userEvent.setup();
    render(<ReleasesPage />);

    expect(await screen.findByText('plugin-demo')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('发布') }));

    // 四级级联逐级选满(命名空间→服务→插件→版本)。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');
    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');
    await openSelect(user, 'pluginId');
    await clickOption(user, 'plugin-demo');
    await openSelect(user, 'pluginVersionId');
    await clickOption(user, '1.2.0');

    // 提交 → publish 抛 502。
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));
    await waitFor(() => expect(publish).toHaveBeenCalled());

    // A2 关键:非 401 失败有通用兜底可见提示(不静默吞)。
    expect(await screen.findByText((t) => t.includes('发布失败'))).toBeInTheDocument();
    // 抽屉不关闭:发布表单标题仍在(handlePublish 返回 false)。
    expect(screen.getByText('发布插件版本')).toBeInTheDocument();
  });
});
