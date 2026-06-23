import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ImagesPage from '../ImagesPage';
import { NamespaceContext, type SelectedNamespace } from '../../context/NamespaceContext';

// mock 资源层:镜像配置(二级页)数据访问走 ../../api/resources。
//  - list('services')                 → 顶部「选服务」下拉
//  - listServiceImages(serviceId)     → 选定服务的镜像台账(后端不分页,信封一次回全集)
//  - setCurrentImage(serviceId, image) → 设为当前 / 新增并设为当前(后端无纯追加端点)
const list = vi.fn();
const listServiceImages = vi.fn();
const setCurrentImage = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
  listServiceImages: (...a: unknown[]) => listServiceImages(...a),
  setCurrentImage: (...a: unknown[]) => setCurrentImage(...a),
}));

// ImagesPage 用 useNamespace();用受控 NamespaceContext 喂定全局 ns。默认「全部命名空间」(null);
// 传 namespace 模拟「选了某具体 ns」以验证服务下拉收窄。
const renderPage = (namespace: SelectedNamespace | null = null): RenderResult =>
  render(
    <NamespaceContext.Provider
      value={{ namespace, setNamespace: () => {}, options: [], optionsLoading: false }}
    >
      <ImagesPage />
    </NamespaceContext.Provider>,
  );

// jsdom 不实现以下 API,antd Select/Modal/Popconfirm(虚拟列表/portal/滚动锁)会调用,补最小 stub。
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
const envelope = <T,>(rows: T[]) => ({ count: rows.length, rows, page: 1, pageSize: rows.length, totalPage: 1 });

// 服务列表(顶部选服务):svc-demo(id=2, ns=1) / svc-other(id=20, ns=9)。
const servicesEnvelope = envelope([
  { id: 2, serviceCode: 'svc-demo', namespaceId: 1, namespaceCode: 'ns-admin' },
  { id: 20, serviceCode: 'svc-other', namespaceId: 9, namespaceCode: 'ns-prod' },
]);

// svc-demo 镜像台账:img:1.7.20(当前)、img:1.7.19(历史)。
const imagesEnvelope = envelope([
  { id: 101, serviceId: 2, image: 'oci.example.com/app:1.7.20', isCurrent: true, createdAt: '2026-06-10T10:00:00Z' },
  { id: 100, serviceId: 2, image: 'oci.example.com/app:1.7.19', isCurrent: false, createdAt: '2026-06-01T10:00:00Z' },
]);

// 点 Popconfirm 气泡里的「确认」按钮。行内触发按钮(<Button type="link">设为当前)与气泡确认按钮文案
// 相同(都叫「设为当前」),按 role+name 会撞两个;故先等气泡(.ant-popover)出现,再在其内取确认按钮。
const confirmPopconfirm = async (user: ReturnType<typeof userEvent.setup>, okText: string) => {
  const popover = await waitFor(() => {
    const el = document.querySelector('.ant-popover:not(.ant-popover-hidden)');
    if (!el) throw new Error('Popconfirm 气泡未出现');
    return el as HTMLElement;
  });
  const btn = await within(popover).findByRole('button', { name: byNormalizedName(okText) });
  await user.click(btn);
};

// 打开顶部服务下拉并选中目标(下拉项渲染在 portal 的 .ant-select-item-option-content)。
const selectService = async (user: ReturnType<typeof userEvent.setup>, contains: string) => {
  const combobox = await screen.findByRole('combobox');
  await user.click(combobox);
  const option = await screen.findByText(
    (_t, node) =>
      node?.classList.contains('ant-select-item-option-content') === true &&
      node.textContent?.includes(contains) === true,
  );
  await user.click(option);
};

describe('ImagesPage(镜像配置二级页)', () => {
  beforeEach(() => {
    list.mockReset();
    listServiceImages.mockReset();
    setCurrentImage.mockReset();
    list.mockImplementation((resource: string) =>
      Promise.resolve(resource === 'services' ? servicesEnvelope : envelope([])),
    );
    listServiceImages.mockResolvedValue(imagesEnvelope);
  });

  it('初始:加载服务列表,未选服务显占位引导(不拉镜像)', async () => {
    renderPage();
    expect(await screen.findByText('请选择一个服务以管理其镜像')).toBeInTheDocument();
    await waitFor(() => expect(list).toHaveBeenCalledWith('services', expect.anything()));
    // 未选服务:不应拉镜像台账。
    expect(listServiceImages).not.toHaveBeenCalled();
  });

  it('选服务 → 拉该服务镜像台账并渲染镜像 + 当前/历史 Tag', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');

    await selectService(user, 'svc-demo');

    // 关键:选服务后按 serviceId 拉镜像台账。
    await waitFor(() => expect(listServiceImages).toHaveBeenCalledWith(2));

    // 两个镜像都渲染;当前镜像所在行有「当前」Tag(该行「是否当前」列 Tag + 操作列置灰「当前」共两处),
    // 历史行有「历史」Tag(唯一)。
    expect(await screen.findByText('oci.example.com/app:1.7.20')).toBeInTheDocument();
    expect(screen.getByText('oci.example.com/app:1.7.19')).toBeInTheDocument();
    // 当前镜像行:「是否当前」列的「当前」Tag。
    const currentRow = screen.getByText('oci.example.com/app:1.7.20').closest('tr')!;
    expect(within(currentRow).getAllByText('当前').length).toBeGreaterThanOrEqual(1);
    // 历史镜像行:「历史」Tag(唯一)。
    expect(screen.getByText('历史')).toBeInTheDocument();
  });

  it('设为当前:历史行「设为当前」→ Popconfirm 确认 → setCurrentImage(serviceId, image)', async () => {
    setCurrentImage.mockResolvedValue({ id: 100, serviceId: 2, image: 'oci.example.com/app:1.7.19', isCurrent: true, createdAt: '' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');
    await selectService(user, 'svc-demo');

    // 定位历史镜像所在行的「设为当前」。
    const cell = await screen.findByText('oci.example.com/app:1.7.19');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('设为当前'));

    // Popconfirm 气泡确认按钮(okText=设为当前);点它触发 setCurrentImage。
    await confirmPopconfirm(user, '设为当前');

    await waitFor(() =>
      expect(setCurrentImage).toHaveBeenCalledWith(2, 'oci.example.com/app:1.7.19'),
    );
  });

  it('新增并设为当前:打开弹窗 → 输入 image → 提交调 setCurrentImage(serviceId, image)', async () => {
    setCurrentImage.mockResolvedValue({ id: 102, serviceId: 2, image: 'oci.example.com/app:2.0.0', isCurrent: true, createdAt: '' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');
    await selectService(user, 'svc-demo');
    await screen.findByText('oci.example.com/app:1.7.20');

    // 打开「新增并设为当前」弹窗。
    await user.click(screen.getByRole('button', { name: byNormalizedName('新增并设为当前') }));

    // 输入镜像地址。
    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByPlaceholderText(byNormalizedName('oci.example.com') as never);
    await user.type(input, 'oci.example.com/app:2.0.0');

    // 点弹窗「新增并设为当前」确认。
    await user.click(within(dialog).getByRole('button', { name: byNormalizedName('新增并设为当前') }));

    // 关键断言:新增即 set-current(后端无纯追加端点)。
    await waitFor(() =>
      expect(setCurrentImage).toHaveBeenCalledWith(2, 'oci.example.com/app:2.0.0'),
    );
  });

  it('新增弹窗:image 为空时确认按钮禁用(不发空 image)', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');
    await selectService(user, 'svc-demo');
    await screen.findByText('oci.example.com/app:1.7.20');

    await user.click(screen.getByRole('button', { name: byNormalizedName('新增并设为当前') }));
    const dialog = await screen.findByRole('dialog');
    // 未输入 → 弹窗内确认按钮禁用。
    const okBtn = within(dialog).getByRole('button', { name: byNormalizedName('新增并设为当前') });
    expect(okBtn).toBeDisabled();
  });

  it('全局命名空间收窄服务下拉:选了具体 ns 时仅列该 ns 的服务', async () => {
    const user = userEvent.setup();
    // 受控全局 ns = {id:1}(ns-admin)→ 只 svc-demo(namespaceId=1)在下拉,svc-other(9)不在。
    renderPage({ id: 1, code: 'ns-admin' });
    await screen.findByText('请选择一个服务以管理其镜像');

    const combobox = await screen.findByRole('combobox');
    await user.click(combobox);

    // 下拉只含 svc-demo,不含 svc-other。
    await waitFor(() => {
      const opts = Array.from(document.querySelectorAll('.ant-select-item-option-content')).map(
        (o) => o.textContent,
      );
      expect(opts.some((t) => t?.includes('svc-demo'))).toBe(true);
      expect(opts.some((t) => t?.includes('svc-other'))).toBe(false);
    });
  });

  it('空态:服务无镜像 → 显「该服务暂无镜像」', async () => {
    listServiceImages.mockResolvedValue(envelope([]));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');
    await selectService(user, 'svc-demo');

    expect(await screen.findByText((t) => t.includes('该服务暂无镜像'))).toBeInTheDocument();
  });

  it('错误态:拉镜像失败 → 显错误 + 重试,点重试重新拉取', async () => {
    let failNext = true;
    listServiceImages.mockImplementation(() => {
      if (failNext) {
        failNext = false;
        return Promise.reject(new Error('boom'));
      }
      return Promise.resolve(imagesEnvelope);
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');
    await selectService(user, 'svc-demo');

    expect(await screen.findByText('加载该服务的镜像台账失败')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('重试') }));
    expect(await screen.findByText('oci.example.com/app:1.7.20')).toBeInTheDocument();
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
      return Promise.resolve(envelope([]));
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('加载服务列表失败')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: byNormalizedName('重试') }));
    expect(await screen.findByText('请选择一个服务以管理其镜像')).toBeInTheDocument();
  });

  it('设为当前失败 → 不抛错(由全局拦截器兜底),行仍在', async () => {
    setCurrentImage.mockRejectedValue({ response: { status: 500 } });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');
    await selectService(user, 'svc-demo');

    const cell = await screen.findByText('oci.example.com/app:1.7.19');
    const row = cell.closest('tr')!;
    await user.click(within(row).getByText('设为当前'));
    await confirmPopconfirm(user, '设为当前');

    await waitFor(() => expect(setCurrentImage).toHaveBeenCalledWith(2, 'oci.example.com/app:1.7.19'));
    // 失败后该行仍在(未误删 UI)。
    expect(screen.getByText('oci.example.com/app:1.7.19')).toBeInTheDocument();
  });

  it('B4 契约:services 列表调用 pageSize ≤ 200(后端硬卡,>200 会 422)', async () => {
    renderPage();
    await screen.findByText('请选择一个服务以管理其镜像');
    await waitFor(() => expect(list).toHaveBeenCalledWith('services', expect.anything()));
    for (const call of list.mock.calls) {
      const ps = (call[call.length - 1] as { pageSize?: number } | undefined)?.pageSize;
      if (ps !== undefined) expect(ps).toBeLessThanOrEqual(200);
    }
  });
});
