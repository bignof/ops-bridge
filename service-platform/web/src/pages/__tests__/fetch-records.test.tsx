import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FetchRecordsPage from '../FetchRecordsPage';

// mock 资源层:获取记录页只读,数据访问走 ../../api/resources:
//  - list('fetch-records', ...) → 列表(服务端分页 + ?namespaceId=/?serviceId= 过滤)
//  - list('namespaces'|'services') → B2 筛选区下拉选项
const list = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
}));

// jsdom 不实现以下 API,ProTable / antd 渲染会调用,补最小 stub 保证用例稳定。
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

// 列表信封:列直接用后端 JOIN 回的可读名(基线 §7)。
// ⚠️ 服务列回 serviceCode(非 serviceName),断言据此校验服务列渲染的是 serviceCode。
// C1:fetchDate 用真实 ISO8601(带 T/Z,后端 datetime 序列化),断言列经 valueType 'dateTime' 格式化(不直显 ISO)。
// C2:对齐后端真实 FetchRecordOut 含 remark(str|None),列渲染备注;mock 不注入后端不回字段。
const fetchRecordsEnvelope = {
  count: 1,
  rows: [
    {
      id: 7,
      namespaceCode: 'ns-demo',
      serviceCode: 'svc-demo',
      pluginCode: 'plugin-demo',
      version: '1.2.0',
      fetchDate: '2026-06-20T10:00:00Z',
      remark: '节点首次拉取',
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

// B2 筛选区下拉选项信封。
const namespacesEnvelope = {
  count: 1,
  rows: [{ id: 1, code: 'ns-demo', name: '演示命名空间' }],
  page: 1,
  pageSize: 500,
  totalPage: 1,
};
const servicesEnvelope = {
  count: 1,
  rows: [{ id: 2, serviceCode: 'svc-demo', name: '演示服务' }],
  page: 1,
  pageSize: 500,
  totalPage: 1,
};

// 按 resource 路由 list 返回:fetch-records 回列表行,namespaces/services 回筛选下拉选项。
const routeList = (resource: string) => {
  switch (resource) {
    case 'namespaces':
      return namespacesEnvelope;
    case 'services':
      return servicesEnvelope;
    default:
      return fetchRecordsEnvelope;
  }
};

// 打开某个筛选项 Select 并展开下拉(按 combobox id 定位)。
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

const byNormalizedName = (text: string) => (name: string) => name.replace(/\s/g, '').includes(text);

describe('FetchRecordsPage', () => {
  beforeEach(() => {
    list.mockReset();
    list.mockImplementation((resource: string) => Promise.resolve(routeList(resource)));
  });

  it('只读列表渲染:列用后端可读名 namespaceCode/serviceCode/pluginCode/version/fetchDate + remark', async () => {
    render(<FetchRecordsPage />);

    // 列直接用后端 JOIN 回的可读名(服务列=serviceCode,不是空的 serviceName)。
    expect(await screen.findByText('svc-demo')).toBeInTheDocument();
    expect(screen.getByText('ns-demo')).toBeInTheDocument();
    expect(screen.getByText('plugin-demo')).toBeInTheDocument();
    expect(screen.getByText('1.2.0')).toBeInTheDocument();
    // C2:备注列渲染后端 FetchRecordOut.remark。
    expect(screen.getByText('节点首次拉取')).toBeInTheDocument();

    // C1:获取时间经 valueType 'dateTime' 格式化为 YYYY-MM-DD HH:mm:ss(本地时区),不直显原始 ISO。
    // 原始 ISO(带 T/Z)绝不出现;格式化后的「日期 时间」串出现(时区无关,用正则匹配格式)。
    expect(screen.queryByText('2026-06-20T10:00:00Z')).not.toBeInTheDocument();
    expect(
      screen.getByText((t) => /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(t)),
    ).toBeInTheDocument();
  });

  it('走 resources.list("fetch-records") 且服务端分页:参数含 page/pageSize', async () => {
    render(<FetchRecordsPage />);
    await screen.findByText('svc-demo');

    // 关键断言:列表调 list('fetch-records', ...) 且把 ProTable current/pageSize 映射成后端 page/pageSize
    // —— 服务端分页,勿全量返回。
    await waitFor(() => expect(list).toHaveBeenCalledWith('fetch-records', expect.anything()));
    const [resource, params] = list.mock.calls[0] as [string, Record<string, unknown>];
    expect(resource).toBe('fetch-records');
    expect(params.page).toBeDefined();
    expect(params.pageSize).toBeDefined();
  });

  it('纯只读:无「添加」按钮、无行内编辑/删除', async () => {
    render(<FetchRecordsPage />);
    await screen.findByText('svc-demo');

    // 只读审计表(基线 §7「本页无写操作」):不渲染添加按钮,也无行内编辑/删除。
    expect(screen.queryByText('添加')).not.toBeInTheDocument();
    expect(screen.queryByText('编辑')).not.toBeInTheDocument();
    expect(screen.queryByText('删除')).not.toBeInTheDocument();
  });

  // B2(命名空间服务端过滤):基线硬要求。选命名空间筛选项 → 查询 → list('fetch-records',{namespaceId}) 透传后端。
  it('筛选区按命名空间过滤:选筛选项 → list("fetch-records", {namespaceId}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    render(<FetchRecordsPage />);
    await screen.findByText('svc-demo');

    await openSelect(user, 'namespaceId');
    await clickOption(user, 'ns-demo');
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    // B2 关键:筛选值 namespaceId 透传到 list('fetch-records', { namespaceId })(后端 ?namespaceId= 过滤)。
    await waitFor(() => {
      expect(list).toHaveBeenCalledWith(
        'fetch-records',
        expect.objectContaining({ namespaceId: 1 }),
      );
    });
  });

  // B2(服务服务端过滤):基线硬要求。选服务筛选项 → 查询 → list('fetch-records',{serviceId}) 透传后端。
  it('筛选区按服务过滤:选筛选项 → list("fetch-records", {serviceId}) 带后端过滤参数', async () => {
    const user = userEvent.setup();
    render(<FetchRecordsPage />);
    await screen.findByText('svc-demo');

    await openSelect(user, 'serviceId');
    await clickOption(user, 'svc-demo');
    await user.click(screen.getByRole('button', { name: byNormalizedName('查询') }));

    await waitFor(() => {
      expect(list).toHaveBeenCalledWith('fetch-records', expect.objectContaining({ serviceId: 2 }));
    });
  });
});
