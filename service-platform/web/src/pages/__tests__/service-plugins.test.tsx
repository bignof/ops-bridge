import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ServicePluginsPage from '../ServicePluginsPage';

// mock 资源层:服务插件页所有数据访问都走 ../../api/resources。
// list 按 resource 路由:
//  - 'service-plugins' → ProTable 列表行(列用后端 JOIN 回的可读名)
//  - 'namespaces'      → 命名空间关联选择 options
//  - 'services'        → 服务级联 options(**必须带 ?namespaceId= 服务端过滤参数**)
//  - 'plugins'         → 插件级联 options
const list = vi.fn();
const create = vi.fn();
const update = vi.fn();
const remove = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
  create: (...a: unknown[]) => create(...a),
  update: (...a: unknown[]) => update(...a),
  remove: (...a: unknown[]) => remove(...a),
}));

// antd 在两个汉字按钮间插空格,带图标按钮的 accessible name 还含图标名;抹空白后用 includes 匹配。
const byNormalizedName = (text: string) => (name: string) => name.replace(/\s/g, '').includes(text);

// jsdom 不实现以下 API,antd Select 的虚拟列表/滚动会调用,补最小 stub 保证下拉可交互、用例稳定。
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

// 列表信封:列直接用后端 JOIN 回的 namespaceCode / serviceCode / pluginCode(不客户端拼 id→名)。
// P1a service-plugins list 关联回 serviceCode(非 serviceName),服务列据此渲染。
const servicePluginsEnvelope = {
  count: 1,
  rows: [
    {
      id: 11,
      namespaceCode: 'ns-demo',
      serviceCode: 'svc-demo',
      pluginCode: 'plugin-demo',
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
// 据 params.namespaceId 返回对应服务,使「换命名空间后服务列表变化 + 旧选值必须被清」可断言。
const servicesByNamespace: Record<number, typeof servicePluginsEnvelope> = {
  1: {
    count: 1,
    rows: [{ id: 2, serviceCode: 'svc-demo', name: '演示服务' }],
    page: 1,
    pageSize: 100,
    totalPage: 1,
  } as never,
  10: {
    count: 1,
    rows: [{ id: 20, serviceCode: 'svc-other', name: '另一服务' }],
    page: 1,
    pageSize: 100,
    totalPage: 1,
  } as never,
};

const pluginsEnvelope = {
  count: 1,
  rows: [{ id: 3, code: 'plugin-demo', name: '演示插件' }],
  page: 1,
  pageSize: 100,
  totalPage: 1,
};

// 服务全量(B2 筛选区 serviceId 下拉不带 namespaceId 拉全量;含 svc-demo id=2 供筛选断言)。
const allServicesEnvelope = {
  count: 2,
  rows: [
    { id: 2, serviceCode: 'svc-demo', name: '演示服务' },
    { id: 20, serviceCode: 'svc-other', name: '另一服务' },
  ],
  page: 1,
  pageSize: 100,
  totalPage: 1,
} as never;

// 按 resource 路由 list 返回;services 再按 namespaceId 服务端过滤分流。
const routeList = (resource: string, params?: Record<string, unknown>) => {
  switch (resource) {
    case 'namespaces':
      return namespacesEnvelope;
    case 'services': {
      // 带 namespaceId:表单级联(按命名空间隔离);不带:B2 筛选区拉全量服务。
      if (params?.namespaceId === undefined) return allServicesEnvelope;
      const nsId = Number(params.namespaceId);
      return servicesByNamespace[nsId] ?? { count: 0, rows: [], page: 1, pageSize: 100, totalPage: 1 };
    }
    case 'plugins':
      return pluginsEnvelope;
    default:
      return servicePluginsEnvelope;
  }
};

// 打开某个表单 Select 并展开下拉。
// 用 form-item 的 combobox `id`(= dataIndex,如 'namespaceId')定位 —— 表头列名也叫「命名空间」,
// 按文案找会撞表头;按 id 唯一锁定表单字段的下拉,稳。
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

describe('ServicePluginsPage', () => {
  beforeEach(() => {
    list.mockReset();
    create.mockReset();
    update.mockReset();
    remove.mockReset();
    list.mockImplementation((resource: string, params?: Record<string, unknown>) =>
      Promise.resolve(routeList(resource, params)),
    );
  });

  it('列表渲染(走 resources.list,列用后端可读名 namespaceCode/serviceCode/pluginCode)', async () => {
    render(<ServicePluginsPage />);
    expect(await screen.findByText('svc-demo')).toBeInTheDocument();
    expect(screen.getByText('ns-demo')).toBeInTheDocument();
    expect(screen.getByText('plugin-demo')).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith('service-plugins', expect.anything());
  });

  it('关联绑定表无编辑:操作列不出现「编辑」(editable=false)', async () => {
    render(<ServicePluginsPage />);
    const cell = await screen.findByText('svc-demo');
    const row = cell.closest('tr')!;
    // 仅增删:行内有「删除」、无「编辑」。
    expect(within(row).getByText('删除')).toBeInTheDocument();
    expect(within(row).queryByText('编辑')).not.toBeInTheDocument();
  });

  it('三级级联走服务端过滤:选命名空间 → list(services,{namespaceId}) → 选服务 → list(plugins)', async () => {
    const user = userEvent.setup();
    render(<ServicePluginsPage />);

    // 列表先加载(确认 ProTable request 走了 resources.list)。
    expect(await screen.findByText('svc-demo')).toBeInTheDocument();

    // 打开新建 Drawer。
    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));

    // 命名空间下拉的选项来自服务端 list('namespaces')(关联选择,非写死)。
    await waitFor(() => {
      expect(list).toHaveBeenCalledWith('namespaces', expect.anything());
    });

    // 选命名空间(下拉选项渲染在 portal,点选项文案 ns-demo)。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');

    // 关键断言:选命名空间后,服务下拉调 list('services', { namespaceId }) —— 带服务端过滤参数,
    // 而非客户端拉全量再 filter。namespaceId 取上面选中的命名空间 id(=1)。
    await waitFor(() => {
      expect(list).toHaveBeenCalledWith('services', expect.objectContaining({ namespaceId: 1 }));
    });

    // 选服务后,插件下拉调 list('plugins')。
    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');

    await waitFor(() => {
      expect(list).toHaveBeenCalledWith('plugins', expect.anything());
    });
  });

  it('点「添加」→ 选满级联 → 提交 → 调 create(service-plugins, {serviceId,pluginId}),C3 裁掉 namespaceId', async () => {
    create.mockResolvedValue({ id: 12 });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);

    expect(await screen.findByText('svc-demo')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));

    // 命名空间。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');

    // 服务(依赖命名空间,选项来自 list('services',{namespaceId}))。
    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');

    // 插件(依赖服务,选项来自 list('plugins'))。
    await openSelect(user, 'pluginId');
    await clickOption(user, 'plugin-demo');

    // 提交。
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));

    // C3:提交体只含 serviceId/pluginId,**不含**仅用于级联的 namespaceId(裁干净,不留隐患)。
    await waitFor(() => {
      expect(create).toHaveBeenCalledWith(
        'service-plugins',
        expect.objectContaining({ serviceId: 2, pluginId: 3 }),
      );
    });
    const payload = create.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload).not.toHaveProperty('namespaceId');
  });

  // B3(409 文案):服务插件无 code 字段,409=重复绑定,文案须为「该插件已绑定该服务…」,非「编码已存在」。
  it('重复绑定 409 → 提示「该插件已绑定该服务,请勿重复关联」(非「编码已存在」)', async () => {
    create.mockRejectedValue({ response: { status: 409 } });
    const user = userEvent.setup();
    render(<ServicePluginsPage />);

    expect(await screen.findByText('svc-demo')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));

    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');
    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');
    await openSelect(user, 'pluginId');
    await clickOption(user, 'plugin-demo');
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));

    // B3 关键:重复绑定的 409 文案贴切,不沿用默认「编码已存在」。
    expect(
      await screen.findByText((t) => t.includes('该插件已绑定该服务')),
    ).toBeInTheDocument();
    expect(screen.queryByText('编码已存在')).not.toBeInTheDocument();
  });

  // B2(按服务服务端过滤):开查询表单 → 选服务筛选项 → 查询 → list('service-plugins',{serviceId}) 透传后端。
  it('筛选区按服务过滤:选筛选项 → list("service-plugins", {serviceId}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    render(<ServicePluginsPage />);

    expect(await screen.findByText('svc-demo')).toBeInTheDocument();

    // 筛选项 id=filterServiceId(避开表单 serviceId 撞 id);选项来自 list('services')。
    await openSelect(user, 'filterServiceId');
    await clickOption(user, 'svc-demo');
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    // B2 关键:筛选值 serviceId 透传到 list('service-plugins', { serviceId })(后端 ?serviceId= 过滤)。
    await waitFor(() => {
      expect(list).toHaveBeenCalledWith(
        'service-plugins',
        expect.objectContaining({ serviceId: 2 }),
      );
    });
  });

  it('A1 级联清空:选命名空间 A → 选其服务 → 改命名空间 B → 服务被清空(不残留 A 的服务,不会错配提交)', async () => {
    const user = userEvent.setup();
    render(<ServicePluginsPage />);

    expect(await screen.findByText('svc-demo')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));

    // 选命名空间 A(ns-demo,id=1)。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');

    // 选 A 的服务(svc-demo,id=2)。
    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');

    // 选中后,serviceId 字段渲染出已选项文案 svc-demo。
    const serviceField = document.getElementById('serviceId')!.closest('.ant-select')!;
    await waitFor(() =>
      expect(serviceField.querySelector('.ant-select-selection-item')?.textContent).toContain(
        'svc-demo',
      ),
    );

    // 改命名空间为 B(ns-other,id=10)。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-other');

    // A1 关键断言:父级变更后,serviceId 已选值被清空 —— 不再残留 A 的 svc-demo
    // (旧实现仅靠 dependencies 重拉选项,值不清 → 提交会带 A 的 serviceId=2 与 B 错配)。
    await waitFor(() =>
      expect(serviceField.querySelector('.ant-select-selection-item')?.textContent ?? '').not.toContain(
        'svc-demo',
      ),
    );
  });
});
