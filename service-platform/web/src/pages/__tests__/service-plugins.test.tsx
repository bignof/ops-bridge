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

// 列表信封:列直接用后端 JOIN 回的 namespaceCode / serviceName / pluginCode(不客户端拼 id→名)。
const servicePluginsEnvelope = {
  count: 1,
  rows: [
    {
      id: 11,
      namespaceCode: 'ns-demo',
      serviceName: 'svc-demo',
      pluginCode: 'plugin-demo',
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

const pluginsEnvelope = {
  count: 1,
  rows: [{ id: 3, code: 'plugin-demo', name: '演示插件' }],
  page: 1,
  pageSize: 100,
  totalPage: 1,
};

// 按 resource 路由 list 返回。
const routeList = (resource: string) => {
  switch (resource) {
    case 'namespaces':
      return namespacesEnvelope;
    case 'services':
      return servicesEnvelope;
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
    list.mockImplementation((resource: string) => Promise.resolve(routeList(resource)));
  });

  it('列表渲染(走 resources.list,列用后端可读名 namespaceCode/serviceName/pluginCode)', async () => {
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

  it('点「添加」→ 选满级联 → 提交 → 调 create(service-plugins, {namespaceId,serviceId,pluginId})', async () => {
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

    await waitFor(() => {
      expect(create).toHaveBeenCalledWith(
        'service-plugins',
        expect.objectContaining({ namespaceId: 1, serviceId: 2, pluginId: 3 }),
      );
    });
  });
});
