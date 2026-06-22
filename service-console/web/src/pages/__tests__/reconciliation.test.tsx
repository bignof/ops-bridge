import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ReconciliationPage from '../ReconciliationPage';

// mock 资源层:对账页数据访问全走 ../../api/resources。
//  - getReconciliation()            → 三态对账(runningButUnmanaged / managedButDown / versionDrift),camelCase
//  - list('namespaces', …)          → 命名空间选项(纳管表单关联选择 + code→id 预选)
//  - create('services', {...})      → 纳管(建 Service),捕获其入参断言预填值正确
const getReconciliation = vi.fn();
const list = vi.fn();
const create = vi.fn();
vi.mock('../../api/resources', () => ({
  getReconciliation: (...a: unknown[]) => getReconciliation(...a),
  list: (...a: unknown[]) => list(...a),
  create: (...a: unknown[]) => create(...a),
}));

// jsdom 不实现以下 API,antd Drawer/Select(滚动锁/虚拟列表/portal)会调用,补最小 stub 保证可交互、用例稳定。
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

// 对账三态信封:严格对齐后端 ReconciliationOut(camelCase),逐字段对齐契约、不伪造字段名/类型。
//  - runningButUnmanaged 行 1(wms-prod):单 agent(ns-prod),实例 2 → 收件箱可纳管。
//  - runningButUnmanaged 行 2(wms-scan):跨双 agent(ns-prod、ns-scan),实例 3 → 命名空间预选首个 + 多空间提示。
//  - managedButDown 行(svc-down / nacos-down / ns-admin):纳管了但没实例(该起没起)。
//  - versionDrift:本期恒空。
const reconWithData = {
  runningButUnmanaged: [
    { nacosService: 'wms-prod', agentIds: ['ns-prod'], instanceCount: 2 },
    { nacosService: 'wms-scan', agentIds: ['ns-prod', 'ns-scan'], instanceCount: 3 },
  ],
  managedButDown: [
    { serviceCode: 'svc-down', nacosServiceName: 'nacos-down', namespaceCode: 'ns-admin' },
  ],
  versionDrift: [],
};

// 全空对账:三态皆空 → 各区块走 Empty 占位。
const reconEmpty = {
  runningButUnmanaged: [],
  managedButDown: [],
  versionDrift: [],
};

// 命名空间选项信封(list('namespaces') 统一信封):ns-prod / ns-scan / ns-admin。
// code → id:ns-prod=10、ns-scan=11、ns-admin=12(纳管时据 agentId(code) 预选 namespaceId)。
const namespacesEnvelope = {
  count: 3,
  rows: [
    { id: 10, code: 'ns-prod', name: '生产' },
    { id: 11, code: 'ns-scan', name: '扫码' },
    { id: 12, code: 'ns-admin', name: '管理' },
  ],
  page: 1,
  pageSize: 200,
  totalPage: 1,
};

describe('ReconciliationPage', () => {
  beforeEach(() => {
    getReconciliation.mockReset();
    list.mockReset();
    create.mockReset();
    getReconciliation.mockResolvedValue(reconWithData);
    list.mockResolvedValue(namespacesEnvelope);
    create.mockResolvedValue({ id: 99 });
  });

  it('渲染三态:收件箱(未纳管)/ 该起没起(managedButDown)/ 版本漂移占位', async () => {
    render(<ReconciliationPage />);

    // 收件箱:两条未纳管服务(nacosService 蓝 tag)。
    expect(await screen.findByText('wms-prod')).toBeInTheDocument();
    expect(screen.getByText('wms-scan')).toBeInTheDocument();
    // 承载命名空间(agentIds)逐个显为 tag:wms-scan 跨 ns-prod / ns-scan。
    const scanRow = screen.getByText('wms-scan').closest('tr')!;
    expect(within(scanRow).getByText('ns-scan')).toBeInTheDocument();

    // 该起没起:managedButDown 行(serviceCode / nacosServiceName / namespaceCode)。
    expect(screen.getByText('svc-down')).toBeInTheDocument();
    expect(screen.getByText('nacos-down')).toBeInTheDocument();
    expect(screen.getByText('ns-admin')).toBeInTheDocument();

    // 版本漂移:本期恒空 → 占位文案。
    expect(screen.getByText((t) => t.includes('待实例携带插件版本'))).toBeInTheDocument();

    // 首屏即拉对账 + 命名空间选项。
    await waitFor(() => expect(getReconciliation).toHaveBeenCalled());
    await waitFor(() => expect(list).toHaveBeenCalledWith('namespaces', expect.anything()));
  });

  it('纳管:点「纳管」开抽屉,预填 serviceCode/nacosServiceName=nacosService、命名空间按 agentId 预选;提交调 create("services",{...}) 带预填值,成功后刷新对账', async () => {
    const user = userEvent.setup();
    render(<ReconciliationPage />);

    // 等收件箱 + 命名空间选项就位(预选依赖 code→id 映射)。
    const prodRow = (await screen.findByText('wms-prod')).closest('tr')!;
    await waitFor(() => expect(list).toHaveBeenCalled());

    // 点该行「纳管」→ 开抽屉。
    await user.click(within(prodRow).getByRole('button', { name: byNormalizedName('纳管') }));

    // 抽屉打开:serviceCode / nacosServiceName 预填 = nacosService('wms-prod')。
    const serviceCodeInput = (await waitFor(() => {
      const el = document.getElementById('serviceCode');
      if (!el) throw new Error('serviceCode 输入框未渲染');
      return el;
    })) as HTMLInputElement;
    const nacosInput = document.getElementById('nacosServiceName') as HTMLInputElement;
    expect(serviceCodeInput.value).toBe('wms-prod');
    expect(nacosInput.value).toBe('wms-prod');

    // 命名空间预选:agentId 'ns-prod' → id 10,Select 显示其 code 标签。
    await waitFor(() => {
      const combo = document.getElementById('namespaceId')?.closest('.ant-select');
      expect(combo?.querySelector('.ant-select-selection-item')?.textContent).toContain('ns-prod');
    });

    // 提交「纳管」(抽屉 footer 按钮)。Drawer 渲染在 portal,按可见文案锁定 footer 的纳管按钮。
    const adoptBtns = screen.getAllByRole('button', { name: byNormalizedName('纳管') });
    await user.click(adoptBtns[adoptBtns.length - 1]);

    // 关键:create('services', {...}) 带正确预填值(namespaceId=10、serviceCode/nacosServiceName='wms-prod')。
    await waitFor(() =>
      expect(create).toHaveBeenCalledWith(
        'services',
        expect.objectContaining({
          namespaceId: 10,
          serviceCode: 'wms-prod',
          nacosServiceName: 'wms-prod',
        }),
      ),
    );
    // 成功后刷新对账(getReconciliation 至少调两次:首屏 + 纳管后)。
    await waitFor(() => expect(getReconciliation.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('纳管成功 toast:提示「已纳管 <serviceCode>」', async () => {
    const user = userEvent.setup();
    render(<ReconciliationPage />);
    const prodRow = (await screen.findByText('wms-prod')).closest('tr')!;
    await waitFor(() => expect(list).toHaveBeenCalled());

    await user.click(within(prodRow).getByRole('button', { name: byNormalizedName('纳管') }));
    await waitFor(() => expect(document.getElementById('serviceCode')).toBeInTheDocument());
    const adoptBtns = screen.getAllByRole('button', { name: byNormalizedName('纳管') });
    await user.click(adoptBtns[adoptBtns.length - 1]);

    // 成功提示含 serviceCode。
    expect(await screen.findByText((t) => t.includes('已纳管 wms-prod'))).toBeInTheDocument();
  });

  it('纳管失败 409:提示「该命名空间下服务编码已存在」,不静默吞、不刷新对账', async () => {
    create.mockRejectedValue({ response: { status: 409 } });
    const user = userEvent.setup();
    render(<ReconciliationPage />);
    const prodRow = (await screen.findByText('wms-prod')).closest('tr')!;
    await waitFor(() => expect(list).toHaveBeenCalled());

    await user.click(within(prodRow).getByRole('button', { name: byNormalizedName('纳管') }));
    await waitFor(() => expect(document.getElementById('serviceCode')).toBeInTheDocument());
    const adoptBtns = screen.getAllByRole('button', { name: byNormalizedName('纳管') });
    await user.click(adoptBtns[adoptBtns.length - 1]);

    await waitFor(() => expect(create).toHaveBeenCalled());
    // 409 专属文案可见(A2:非 401 失败必须有可见提示)。
    expect(await screen.findByText('该命名空间下服务编码已存在')).toBeInTheDocument();
    // 失败不刷新对账:getReconciliation 仍只首屏调过 1 次。
    expect(getReconciliation).toHaveBeenCalledTimes(1);
  });

  it('空态:三态皆空 → 收件箱/该起没起各显空占位,不崩', async () => {
    getReconciliation.mockResolvedValue(reconEmpty);
    render(<ReconciliationPage />);

    // 收件箱空占位。
    expect(await screen.findByText('无未纳管服务 —— 都已纳管')).toBeInTheDocument();
    // managedButDown 空占位。
    expect(screen.getByText('无异常 —— 已纳管服务均有活跃实例')).toBeInTheDocument();
    // 无任何收件箱数据行。
    expect(screen.queryByText('wms-prod')).not.toBeInTheDocument();
  });

  it('错误态:getReconciliation 失败 → 显加载失败 + 重试按钮,点重试重新拉取', async () => {
    getReconciliation.mockRejectedValueOnce({ response: { status: 500 } });
    const user = userEvent.setup();
    render(<ReconciliationPage />);

    // 错误态文案 + 重试按钮(不静默卡空白)。
    expect(await screen.findByText('加载对账数据失败')).toBeInTheDocument();
    const retry = screen.getByRole('button', { name: byNormalizedName('重试') });

    // 点重试 → 第二次 resolve(reconWithData)→ 渲染收件箱。
    await user.click(retry);
    expect(await screen.findByText('wms-prod')).toBeInTheDocument();
  });

  it('刷新:点「刷新」按钮重新拉取对账', async () => {
    const user = userEvent.setup();
    render(<ReconciliationPage />);
    await screen.findByText('wms-prod');
    await waitFor(() => expect(getReconciliation).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole('button', { name: byNormalizedName('刷新') }));
    await waitFor(() => expect(getReconciliation.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('跨多命名空间:wms-scan(双 agent)纳管抽屉显多空间提示,命名空间预选首个 agentId(ns-prod→10)', async () => {
    const user = userEvent.setup();
    render(<ReconciliationPage />);
    const scanRow = (await screen.findByText('wms-scan')).closest('tr')!;
    await waitFor(() => expect(list).toHaveBeenCalled());

    await user.click(within(scanRow).getByRole('button', { name: byNormalizedName('纳管') }));

    // 预填 serviceCode = nacosService('wms-scan')。
    const serviceCodeInput = (await waitFor(() => {
      const el = document.getElementById('serviceCode');
      if (!el) throw new Error('serviceCode 输入框未渲染');
      return el;
    })) as HTMLInputElement;
    expect(serviceCodeInput.value).toBe('wms-scan');

    // 跨多命名空间:抽屉给出「该服务跨多个命名空间」提示(含两个 agentId)。
    expect(screen.getByText((t) => t.includes('该服务跨多个命名空间'))).toBeInTheDocument();

    // 命名空间仍按首个 agentId(ns-prod)预选。
    await waitFor(() => {
      const combo = document.getElementById('namespaceId')?.closest('.ant-select');
      expect(combo?.querySelector('.ant-select-selection-item')?.textContent).toContain('ns-prod');
    });
  });

  it('agentId 未登记命名空间台账:不预选命名空间(留空待手选),提交校验拦截缺命名空间', async () => {
    // 收件箱行的 agentId('ns-ghost')不在命名空间台账里 → code→id 解析不到,namespaceId 留空。
    getReconciliation.mockResolvedValue({
      runningButUnmanaged: [{ nacosService: 'ghost-svc', agentIds: ['ns-ghost'], instanceCount: 1 }],
      managedButDown: [],
      versionDrift: [],
    });
    const user = userEvent.setup();
    render(<ReconciliationPage />);
    const row = (await screen.findByText('ghost-svc')).closest('tr')!;
    await waitFor(() => expect(list).toHaveBeenCalled());

    await user.click(within(row).getByRole('button', { name: byNormalizedName('纳管') }));
    await waitFor(() => expect(document.getElementById('serviceCode')).toBeInTheDocument());

    // 命名空间未预选(解析不到 ns-ghost):无 selection-item 文本。
    const combo = document.getElementById('namespaceId')?.closest('.ant-select');
    expect(combo?.querySelector('.ant-select-selection-item')?.textContent ?? '').not.toContain(
      'ns-ghost',
    );

    // 直接提交:命名空间必填校验拦截,不调 create(校验文案与 placeholder 不同字,避免误中)。
    const adoptBtns = screen.getAllByRole('button', { name: byNormalizedName('纳管') });
    await user.click(adoptBtns[adoptBtns.length - 1]);
    expect(await screen.findByText('请选择要纳管到的命名空间')).toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();
  });

  it('命名空间台账为空:纳管抽屉命名空间下拉显「无可选命名空间」(引导先去创建)', async () => {
    list.mockResolvedValue({ count: 0, rows: [], page: 1, pageSize: 200, totalPage: 0 });
    const user = userEvent.setup();
    render(<ReconciliationPage />);
    const prodRow = (await screen.findByText('wms-prod')).closest('tr')!;
    await waitFor(() => expect(list).toHaveBeenCalled());

    await user.click(within(prodRow).getByRole('button', { name: byNormalizedName('纳管') }));
    await waitFor(() => expect(document.getElementById('serviceCode')).toBeInTheDocument());

    // 展开命名空间下拉 → 空选项时显引导文案。
    const combo = document.getElementById('namespaceId')!;
    await user.click(combo);
    expect(await screen.findByText('无可选命名空间')).toBeInTheDocument();
  });

  it('managedButDown 命名空间可空:namespaceCode=null 时列显「-」', async () => {
    getReconciliation.mockResolvedValue({
      runningButUnmanaged: [],
      managedButDown: [
        { serviceCode: 'svc-nons', nacosServiceName: 'nacos-nons', namespaceCode: null },
      ],
      versionDrift: [],
    });
    render(<ReconciliationPage />);

    const row = (await screen.findByText('svc-nons')).closest('tr')!;
    // namespaceCode=null → 「-」。
    expect(within(row).getByText('-')).toBeInTheDocument();
  });
});
