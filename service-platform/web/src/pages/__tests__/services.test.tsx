import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ServicesPage from '../ServicesPage';

// mock 资源层:服务页所有数据访问都走 ../../api/resources。
// list 既服务于 ProTable 列表(resource='services'),又服务于表单 namespaceId 关联选择(resource='namespaces')。
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

// 打开某个表单 Select 并展开下拉(按 combobox `id`(= dataIndex)定位:表头列名也可能撞,按 id 唯一锁定)。
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

// 列表信封:列直接用后端 JOIN 回的 namespaceCode(不客户端拼 id→名)。
// ⚠️ 对齐后端真实 ServiceOut:含 namespaceId(C5 回填关联选择必需的隐性契约,后端 list 须回 id 列)+
//    defaultImage(C5 回填断言)。mock 不注入后端不回的字段。
const servicesEnvelope = {
  count: 1,
  rows: [
    {
      id: 5,
      namespaceId: 1,
      namespaceCode: 'ns-demo',
      serviceCode: 'svc-demo',
      name: '演示服务',
      dir: '/opt/svc',
      defaultImage: 'img:1',
      nacosServiceName: 'svc-nacos',
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

// 按 resource 路由 list 返回:'services' 回列表行,'namespaces' 回关联选择 options。
const routeList = (resource: string) =>
  resource === 'namespaces' ? namespacesEnvelope : servicesEnvelope;

describe('ServicesPage', () => {
  beforeEach(() => {
    list.mockReset();
    create.mockReset();
    update.mockReset();
    remove.mockReset();
    list.mockImplementation((resource: string) => Promise.resolve(routeList(resource)));
  });

  it('列表渲染(走 resources.list,列用后端可读名 namespaceCode)', async () => {
    render(<ServicesPage />);
    expect(await screen.findByText('svc-demo')).toBeInTheDocument();
    // 命名空间列直接展示后端 JOIN 回的 namespaceCode。
    expect(screen.getByText('ns-demo')).toBeInTheDocument();
    expect(screen.getByText('svc-nacos')).toBeInTheDocument();
  });

  it('点「添加」→ 命名空间关联选择拉 list(namespaces) → 填编码 → 提交 → 调 create', async () => {
    create.mockResolvedValue({ id: 6, serviceCode: 'svc-new' });
    const user = userEvent.setup();
    render(<ServicesPage />);

    expect(await screen.findByText('svc-demo')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: byNormalizedName('添加') }));

    // 命名空间下拉的选项来自服务端 list('namespaces')(关联选择,非写死)。
    await waitFor(() => {
      expect(list).toHaveBeenCalledWith('namespaces', expect.anything());
    });

    // 选命名空间(必填,关联选择)—— 不选则 DrawerForm 校验拦截、不会调 create。
    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');

    // 填服务编码(必填)后提交。
    await user.type(await screen.findByLabelText('服务编码'), 'svc-new');
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));

    await waitFor(() => {
      expect(create).toHaveBeenCalledWith(
        'services',
        expect.objectContaining({ namespaceId: 1, serviceCode: 'svc-new' }),
      );
    });
  });

  // C5(编辑回填关联字段):渲染 → 点行内「编辑」→ 断言 namespaceId select 预填后端行的当前命名空间、
  // dir/defaultImage 输入框预填行值 → 改一字段提交 → 断言 update 收到合并 values(守「后端 list 须回 id 列供回填」契约)。
  it('点行内「编辑」→ 关联字段(namespaceId)与文本字段(dir)按行值预填 → 改一字段 → 提交 update 合并 values', async () => {
    update.mockResolvedValue({ id: 5 });
    const user = userEvent.setup();
    render(<ServicesPage />);

    // 行加载(后端 list 行须含 namespaceId 供回填关联选择)。
    const cell = await screen.findByText('svc-demo');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('编辑'));

    // C5 关键:namespaceId 关联选择按行 namespaceId(=1)回填,显示其 code「ns-demo」。
    const nsField = await waitFor(() => {
      const el = document.getElementById('namespaceId');
      if (!el) throw new Error('namespaceId 未渲染');
      return el.closest('.ant-select')!;
    });
    await waitFor(() =>
      expect(nsField.querySelector('.ant-select-selection-item')?.textContent).toContain('ns-demo'),
    );

    // 文本字段按行值回填:目录预填 '/opt/svc'、默认镜像预填 'img:1'(后端行携带)。
    expect((await screen.findByLabelText('目录')).getAttribute('value')).toBe('/opt/svc');
    expect(screen.getByLabelText('默认镜像').getAttribute('value')).toBe('img:1');

    // 改目录后提交。
    const dirInput = screen.getByLabelText('目录');
    await user.clear(dirInput);
    await user.type(dirInput, '/opt/new');
    await user.click(screen.getByRole('button', { name: byNormalizedName('确认') }));

    // update 收到合并后的 values:id 定位 + namespaceId 维持原值 + dir 为改后值。
    await waitFor(() => {
      expect(update).toHaveBeenCalledWith(
        'services',
        5,
        expect.objectContaining({ namespaceId: 1, dir: '/opt/new', serviceCode: 'svc-demo' }),
      );
    });
  });

  // B2(按命名空间服务端过滤):开查询表单 → 选命名空间筛选项 → 提交 → list('services',{namespaceId}) 透传后端。
  it('筛选区按命名空间过滤:选筛选项 → list("services", {namespaceId}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    render(<ServicesPage />);

    expect(await screen.findByText('svc-demo')).toBeInTheDocument();

    // 筛选区命名空间下拉选项来自 list('namespaces');筛选项 id=filterNamespaceId(避开表单 namespaceId 撞 id)。
    await openSelect(user, 'filterNamespaceId');
    await clickOption(user, 'ns-demo');

    // 点查询表单的「查询」按钮触发 request。
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    // B2 关键:列表 request 把筛选值 namespaceId 透传到 list('services', { namespaceId })(后端 ?namespaceId= 过滤)。
    await waitFor(() => {
      expect(list).toHaveBeenCalledWith('services', expect.objectContaining({ namespaceId: 1 }));
    });
  });
});
