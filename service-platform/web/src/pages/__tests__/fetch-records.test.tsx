import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import FetchRecordsPage from '../FetchRecordsPage';

// mock 资源层:获取记录页只读,数据访问只走 ../../api/resources 的 list('fetch-records', ...)。
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
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

describe('FetchRecordsPage', () => {
  beforeEach(() => {
    list.mockReset();
    list.mockResolvedValue(fetchRecordsEnvelope);
  });

  it('只读列表渲染:列用后端可读名 namespaceCode/serviceCode/pluginCode/version/fetchDate', async () => {
    render(<FetchRecordsPage />);

    // 列直接用后端 JOIN 回的可读名(服务列=serviceCode,不是空的 serviceName)。
    expect(await screen.findByText('svc-demo')).toBeInTheDocument();
    expect(screen.getByText('ns-demo')).toBeInTheDocument();
    expect(screen.getByText('plugin-demo')).toBeInTheDocument();
    expect(screen.getByText('1.2.0')).toBeInTheDocument();

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
});
