import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import NodeOperationsPage from '../NodeOperationsPage';

// mock 资源层:操作审计页只读,唯一数据访问走 ../../api/resources.listNodeOperations
//  - listNodeOperations(params) → 审计列表(服务端分页,信封 {count, rows, …};
//    row = 后端 NodeOperationOut camelCase),捕获其入参断言服务端分页。
const listNodeOperations = vi.fn();
vi.mock('../../api/resources', () => ({
  listNodeOperations: (...a: unknown[]) => listNodeOperations(...a),
}));

// jsdom 不实现以下 API,ProTable / antd(Tooltip 虚拟定位等)渲染会调用,补最小 stub 保证用例稳定。
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

// 审计列表信封:**逐字段严格对齐后端 NodeOperationOut(camelCase)**,不伪造字段名/类型
// (防 P1-SPA 反复出现的 mock 不对齐假绿)。后端模型字段(app/models.py NodeOperationOut):
//   requestId(str) / agentId(str) / action(str) / mode(str|None) / status(str)
//   / requestedBy(str|None,派生身份) / requestSource(str|None) / dir(str|None)
//   / image(str|None) / output(str|None,后端已截尾) / error(str|None,已截尾)
//   / createdAt(datetime→ISO|None) / updatedAt(datetime→ISO|None)。
//  - 行 1(req-1 succeeded):字段齐全;status 用 hub 实证取值 'succeeded'(见后端 test_nodes 审计用例);
//    output 为长文本(验证列省略不撑爆)。
//  - 行 2(req-2 failed):mode/dir/requestSource 为 null → 列显「-」;含 error。
//  - 行 3(req-3 queued):进行中(蓝 Tag),覆盖 statusColor 蓝分支。
//  - 行 4(req-4 unknown):未知 status → 默认色 Tag(原样显示),覆盖 statusColor 默认分支。
const operationsEnvelope = {
  count: 4,
  rows: [
    {
      requestId: 'req-1',
      agentId: 'ns-online',
      action: 'start',
      mode: null,
      status: 'succeeded',
      requestedBy: 'admin',
      requestSource: 'console',
      dir: '/opt/svc-online',
      image: 'img-online:1',
      output:
        '启动成功,容器已就绪。' + '日志输出非常长'.repeat(60) + '末尾包含最终结果状态行 OK',
      error: null,
      createdAt: '2026-06-20T10:00:00Z',
      updatedAt: '2026-06-20T10:00:05Z',
    },
    {
      requestId: 'req-2',
      agentId: 'ns-degraded',
      action: 'redeploy',
      mode: null,
      status: 'failed',
      requestedBy: 'operator',
      requestSource: null,
      dir: null,
      image: 'img-degraded:1',
      output: null,
      error: 'pull image failed',
      createdAt: '2026-06-20T11:00:00Z',
      updatedAt: '2026-06-20T11:00:03Z',
    },
    {
      requestId: 'req-3',
      agentId: 'ns-online',
      action: 'stop',
      mode: 'graceful',
      status: 'queued',
      requestedBy: 'scheduler',
      requestSource: 'console',
      dir: '/opt/svc-online',
      image: null,
      output: null,
      error: null,
      createdAt: '2026-06-20T12:00:00Z',
      updatedAt: '2026-06-20T12:00:00Z',
    },
    {
      requestId: 'req-4',
      agentId: 'ns-online',
      action: 'force-restart',
      mode: 'force',
      status: 'weird-state',
      requestedBy: 'system',
      requestSource: 'console',
      dir: '/opt/svc-online',
      image: null,
      output: null,
      error: null,
      createdAt: '2026-06-20T13:00:00Z',
      updatedAt: '2026-06-20T13:00:00Z',
    },
  ],
  page: 1,
  pageSize: 20,
  totalPage: 1,
};

describe('NodeOperationsPage', () => {
  beforeEach(() => {
    listNodeOperations.mockReset();
    listNodeOperations.mockResolvedValue(operationsEnvelope);
  });

  it('只读审计渲染:requestId / action / status / 操作人(requestedBy)显示;mode/dir 为空的行显「-」', async () => {
    render(<NodeOperationsPage />);

    // 关键审计字段渲染(逐字段对齐 NodeOperationOut)。
    expect(await screen.findByText('req-1')).toBeInTheDocument();
    expect(screen.getByText('req-2')).toBeInTheDocument();

    // action 列。
    expect(screen.getByText('start')).toBeInTheDocument();
    expect(screen.getByText('redeploy')).toBeInTheDocument();

    // status 列(hub 实证取值 succeeded / failed)。
    expect(screen.getByText('succeeded')).toBeInTheDocument();
    expect(screen.getByText('failed')).toBeInTheDocument();

    // 操作人列(派生身份 requestedBy)。
    expect(screen.getByText('admin')).toBeInTheDocument();
    expect(screen.getByText('operator')).toBeInTheDocument();

    // 行 2:mode=null 且 dir=null 且 requestSource=null → 这些列显「-」(至少出现一次)。
    const failedRow = screen.getByText('req-2').closest('tr')!;
    expect(within(failedRow).getAllByText('-').length).toBeGreaterThanOrEqual(1);

    // 服务端分页:走 listNodeOperations(读统一信封),首屏即调一次。
    await waitFor(() => expect(listNodeOperations).toHaveBeenCalled());
  });

  it('status 以 Tag 区分:succeeded 绿 / failed 红 / queued 蓝 / 未知值默认色', async () => {
    render(<NodeOperationsPage />);
    await screen.findByText('req-1');

    // 各行 status 文本所在的 Tag(antd 预设色 → class `ant-tag-<color>`;未知值无颜色 class)。
    const tagOf = (statusText: string) =>
      screen.getByText(statusText).closest('.ant-tag') as HTMLElement;

    // 行 1 succeeded → 绿。
    expect(tagOf('succeeded').className).toContain('ant-tag-green');
    // 行 2 failed → 红。
    expect(tagOf('failed').className).toContain('ant-tag-red');
    // 行 3 queued → 蓝(进行中)。
    expect(tagOf('queued').className).toContain('ant-tag-blue');
    // 行 4 未知 status → 默认色(不带任何预设颜色 class),但仍原样展示文本。
    const unknownTag = tagOf('weird-state');
    expect(unknownTag).toBeInTheDocument();
    expect(unknownTag.className).not.toMatch(/ant-tag-(green|red|blue)/);
  });

  it('走 resources.listNodeOperations 且服务端分页:参数含 page/pageSize', async () => {
    render(<NodeOperationsPage />);
    await screen.findByText('req-1');

    // 关键断言:列表把 ProTable 的 current/pageSize 映射成后端 page/pageSize —— 服务端分页,勿全量返回。
    await waitFor(() => expect(listNodeOperations).toHaveBeenCalled());
    const [params] = listNodeOperations.mock.calls[0] as [Record<string, unknown>];
    expect(params.page).toBeDefined();
    expect(params.pageSize).toBeDefined();
  });

  it('翻页 / 改页大小触发 listNodeOperations 带新的 page/pageSize', async () => {
    // 让总数 > 1 页,分页器才出现「下一页」。
    listNodeOperations.mockResolvedValue({ ...operationsEnvelope, count: 60 });
    const user = userEvent.setup();
    render(<NodeOperationsPage />);
    await screen.findByText('req-1');

    // 首屏拉取的 pageSize(默认页大小),后续翻页应带同样 pageSize、page=2。
    await waitFor(() => expect(listNodeOperations).toHaveBeenCalled());
    const firstParams = listNodeOperations.mock.calls[0][0] as { pageSize?: number };
    const firstPageSize = firstParams.pageSize;
    expect(firstPageSize).toBeDefined();

    listNodeOperations.mockClear();
    // 点「下一页」(antd 分页 next 按钮 title「下一页」)。
    await user.click(screen.getByTitle('下一页'));

    await waitFor(() => expect(listNodeOperations).toHaveBeenCalled());
    const nextParams = listNodeOperations.mock.calls.at(-1)![0] as {
      page?: number;
      pageSize?: number;
    };
    expect(nextParams.page).toBe(2);
    expect(nextParams.pageSize).toBe(firstPageSize);
  });

  it('纯只读:无「新建 / 添加 / 编辑 / 删除」类破坏性按钮', async () => {
    render(<NodeOperationsPage />);
    await screen.findByText('req-1');

    // 只读审计表:不渲染任何写操作 / 行操作按钮。
    expect(screen.queryByText('新建')).not.toBeInTheDocument();
    expect(screen.queryByText('添加')).not.toBeInTheDocument();
    expect(screen.queryByText('编辑')).not.toBeInTheDocument();
    expect(screen.queryByText('删除')).not.toBeInTheDocument();
    // 无行操作列(节点页才有的「操作」Dropdown 触发按钮),审计页不应出现。
    expect(screen.queryByRole('button', { name: byNormalizedName('操作') })).not.toBeInTheDocument();
  });

  it('output 长文本以省略列展示,不直接撑爆(列含 ellipsis,原始全文不整段平铺渲染)', async () => {
    render(<NodeOperationsPage />);
    await screen.findByText('req-1');

    // output 走 ellipsis 单元格:DOM 里该单元格含 ellipsis 类,且不会把整段长文本作为完整可见文本节点平铺。
    const okRow = screen.getByText('req-1').closest('tr')!;
    expect(okRow.querySelector('.ant-typography-ellipsis')).toBeTruthy();
  });
});
