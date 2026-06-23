import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import PublishRolloutModal, { type PublishRolloutModalProps } from '../PublishRolloutModal';
import { NamespaceContext, type SelectedNamespace } from '../../context/NamespaceContext';

// mock 资源层:发布弹窗数据访问走 ../../api/resources。
//  - list('services')                 → 服务下拉(取 nacosServiceName/id/namespaceCode)
//  - listServiceImages(serviceId)     → pull-redeploy 预填当前镜像 + 历史下拉
//  - listInstances(params)            → 灰度候选实例(前端按 nacosService 过滤)
//  - createRollout(body)              → 发起投放,捕获 body 做断言
//  - getRollout(id)                   → 进度轮询(含 rollingTask 逐实例进度)
const list = vi.fn();
const listServiceImages = vi.fn();
const listInstances = vi.fn();
const createRollout = vi.fn();
const getRollout = vi.fn();
vi.mock('../../api/resources', () => ({
  list: (...a: unknown[]) => list(...a),
  listServiceImages: (...a: unknown[]) => listServiceImages(...a),
  listInstances: (...a: unknown[]) => listInstances(...a),
  createRollout: (...a: unknown[]) => createRollout(...a),
  getRollout: (...a: unknown[]) => getRollout(...a),
}));

// PublishRolloutModal 用 useNamespace();受控 NamespaceContext 喂定全局 ns。默认「全部命名空间」(null)。
// pollIntervalMs 给小值(20ms)让轮询用例可经 waitFor 观测到第二次拉取(不写死不可控的 2s)。
const renderModal = (
  props: Partial<PublishRolloutModalProps> = {},
  namespace: SelectedNamespace | null = null,
): RenderResult =>
  render(
    <NamespaceContext.Provider
      value={{ namespace, setNamespace: () => {}, options: [], optionsLoading: false }}
    >
      <PublishRolloutModal open onClose={() => {}} pollIntervalMs={20} {...props} />
    </NamespaceContext.Provider>,
  );

// jsdom 不实现以下 API,antd Select/Modal/Table/AutoComplete 会调用,补最小 stub。
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

// 服务列表:svc-demo(id=2, ns=1, nacos=wms-admin) / svc-noNacos(id=3, 无 nacosServiceName → 不可投放)。
const servicesEnvelope = envelope([
  { id: 2, serviceCode: 'svc-demo', namespaceId: 1, namespaceCode: 'ns-admin', nacosServiceName: 'wms-admin' },
  { id: 3, serviceCode: 'svc-nonacos', namespaceId: 1, namespaceCode: 'ns-admin' },
]);

// svc-demo 镜像台账:1.7.20(当前)、1.7.19(历史)。
const imagesEnvelope = envelope([
  { id: 101, serviceId: 2, image: 'oci.example.com/app:1.7.20', isCurrent: true, createdAt: '2026-06-10T10:00:00Z' },
  { id: 100, serviceId: 2, image: 'oci.example.com/app:1.7.19', isCurrent: false, createdAt: '2026-06-01T10:00:00Z' },
]);

// 灰度候选实例(listInstances 回);两个 wms-admin 实例 + 一个别的服务(应被前端按 nacosService 过滤掉)。
const instancesEnvelope = envelope([
  {
    agentId: 'ns-admin', containerName: 'c-admin-1', containerId: 'cid-1', composeProject: 'p1',
    composeService: 's', dir: '/d/1', image: 'img', running: true, nacosService: 'wms-admin',
    healthy: true, status: 'active', heartbeatAt: null, firstSeenAt: null,
  },
  {
    agentId: 'ns-admin', containerName: 'c-admin-2', containerId: 'cid-2', composeProject: 'p2',
    composeService: 's', dir: '/d/2', image: 'img', running: true, nacosService: 'wms-admin',
    healthy: true, status: 'active', heartbeatAt: null, firstSeenAt: null,
  },
  {
    agentId: 'ns-admin', containerName: 'c-other', containerId: 'cid-9', composeProject: 'p9',
    composeService: 's', dir: '/d/9', image: 'img', running: true, nacosService: 'other-svc',
    healthy: true, status: 'active', heartbeatAt: null, firstSeenAt: null,
  },
]);

// 进度详情工厂:给定 status / nodes,组出 RolloutDetail。
const detailOf = (status: string, opts: { frozen?: boolean; mode?: string; nodes?: unknown[] } = {}) => ({
  id: 'ro-1',
  namespace: 'ns-admin',
  serviceName: 'wms-admin',
  mode: opts.mode ?? 'restart',
  trigger: 'manual',
  target: null,
  previousTarget: null,
  status,
  frozen: opts.frozen ?? false,
  rollingTaskId: 'task-1',
  error: null,
  force: false,
  createdAt: '2026-06-20T10:00:00Z',
  finishedAt: status === 'running' ? null : '2026-06-20T10:05:00Z',
  rollingTask: {
    taskId: 'task-1',
    agentId: '*',
    serviceName: 'wms-admin',
    status,
    degraded: false,
    nodes: opts.nodes ?? [{ address: '10.0.0.1:8080', containerId: 'cid-1', status: status === 'running' ? 'in-progress' : 'done' }],
    error: null,
    createdAt: null,
    updatedAt: null,
    finishedAt: null,
  },
});

// 打开服务下拉并选中目标(下拉项在 portal 的 .ant-select-item-option-content)。
const selectService = async (user: ReturnType<typeof userEvent.setup>, contains: string) => {
  // 弹窗内第一个 combobox 即服务下拉。
  const combobox = await screen.findByRole('combobox');
  await user.click(combobox);
  const option = await screen.findByText(
    (_t, node) =>
      node?.classList.contains('ant-select-item-option-content') === true &&
      node.textContent?.includes(contains) === true,
  );
  await user.click(option);
};

describe('PublishRolloutModal(发布投放弹窗)', () => {
  beforeEach(() => {
    list.mockReset();
    listServiceImages.mockReset();
    listInstances.mockReset();
    createRollout.mockReset();
    getRollout.mockReset();
    list.mockImplementation((resource: string) =>
      Promise.resolve(resource === 'services' ? servicesEnvelope : envelope([])),
    );
    listServiceImages.mockResolvedValue(imagesEnvelope);
    listInstances.mockResolvedValue(instancesEnvelope);
    createRollout.mockResolvedValue({ rolloutId: 'ro-1', taskId: 'task-1' });
    getRollout.mockResolvedValue(detailOf('done'));
  });

  it('配置表单渲染:打开即拉服务列表 + 机制/范围 默认(restart / 全部),发起投放按钮初始禁用(未选服务)', async () => {
    renderModal();
    // 配置阶段顶部说明(唯一文案,作配置阶段就绪锚点)+ 机制两选项。
    expect(await screen.findByText('按你的变更类型选择投放机制')).toBeInTheDocument();
    expect(await screen.findByText(byNormalizedName('重启(插件变更)'))).toBeInTheDocument();
    expect(screen.getByText(byNormalizedName('重拉镜像(镜像变更)'))).toBeInTheDocument();
    // 拉服务列表。
    await waitFor(() => expect(list).toHaveBeenCalledWith('services', expect.anything()));
    // 未选服务 → 发起投放禁用。
    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    expect(submit).toBeDisabled();
  });

  it('机制切换:选「重拉镜像」显镜像输入并预填当前镜像;切回「重启」隐藏镜像输入', async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    // 选服务后拉镜像台账。
    await waitFor(() => expect(listServiceImages).toHaveBeenCalledWith(2));

    // restart 默认:无镜像输入(无「镜像」label 对应输入区)。
    expect(screen.queryByText('重拉镜像必须指定镜像(否则无法投放)')).not.toBeInTheDocument();

    // 切「重拉镜像」→ 出现镜像输入,且预填当前镜像 1.7.20(AutoComplete 内部可能多个承载元素,取至少一处)。
    await user.click(screen.getByText(byNormalizedName('重拉镜像(镜像变更)')));
    await waitFor(() =>
      expect(screen.getAllByDisplayValue('oci.example.com/app:1.7.20').length).toBeGreaterThanOrEqual(1),
    );

    // 切回「重启」→ 镜像输入消失。
    await user.click(screen.getByText(byNormalizedName('重启(插件变更)')));
    await waitFor(() =>
      expect(screen.queryAllByDisplayValue('oci.example.com/app:1.7.20').length).toBe(0),
    );
  });

  it('pull-redeploy 无镜像禁止提交:清空镜像 → 发起投放禁用 + 错误提示', async () => {
    // 镜像台账为空(不预填),强制用户手填;不填则禁提交。
    listServiceImages.mockResolvedValue(envelope([]));
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    await user.click(screen.getByText(byNormalizedName('重拉镜像(镜像变更)')));

    // 镜像为空 → 错误提示 + 提交禁用。
    expect(await screen.findByText('重拉镜像必须指定镜像(否则无法投放)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: byNormalizedName('发起投放') })).toBeDisabled();
  });

  it('提交组装 body(restart + 全部实例):createRollout({serviceName=nacos, mode:restart, force})', async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');

    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => expect(createRollout).toHaveBeenCalled());
    const body = createRollout.mock.calls[0][0] as Record<string, unknown>;
    // serviceName = nacosServiceName(不是 serviceCode);mode=restart;namespace=命名空间 code;force=false。
    expect(body.serviceName).toBe('wms-admin');
    expect(body.mode).toBe('restart');
    expect(body.namespace).toBe('ns-admin');
    expect(body.force).toBe(false);
    // restart 不带 image;全部实例不带 instances。
    expect(body.image).toBeUndefined();
    expect(body.instances).toBeUndefined();
  });

  it('提交组装 body(pull-redeploy):带 image,且 target 默认兜底为镜像 tag', async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    await user.click(screen.getByText(byNormalizedName('重拉镜像(镜像变更)')));
    // 预填当前镜像。
    await waitFor(() =>
      expect(screen.getAllByDisplayValue('oci.example.com/app:1.7.20').length).toBeGreaterThanOrEqual(1),
    );

    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => expect(createRollout).toHaveBeenCalled());
    const body = createRollout.mock.calls[0][0] as Record<string, unknown>;
    expect(body.mode).toBe('pull-redeploy');
    expect(body.image).toBe('oci.example.com/app:1.7.20');
    // image 后端不落库 → target 兜底为镜像 tag(审计可识别)。
    expect(body.target).toBe('oci.example.com/app:1.7.20');
  });

  it('灰度:选「灰度」拉候选实例(前端按 nacosService 过滤)→ 勾选实例 → body 带 instances=[containerId]', async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');

    // 选灰度 → 拉候选实例(按命名空间 code 收窄)。
    await user.click(screen.getByText(byNormalizedName('灰度(选实例)')));
    await waitFor(() =>
      expect(listInstances).toHaveBeenCalledWith(expect.objectContaining({ namespace: 'ns-admin' })),
    );
    // 候选只含 wms-admin 的两个实例(other-svc 被前端过滤掉)。
    expect(await screen.findByText('c-admin-1')).toBeInTheDocument();
    expect(screen.getByText('c-admin-2')).toBeInTheDocument();
    expect(screen.queryByText('c-other')).not.toBeInTheDocument();

    // 未选实例 → 提交禁用 + 提示。
    expect(screen.getByText('请至少选择一个实例。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: byNormalizedName('发起投放') })).toBeDisabled();

    // 勾第一个实例(行内 checkbox)。
    const row = screen.getByText('c-admin-1').closest('tr')!;
    await user.click(within(row).getByRole('checkbox'));

    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => expect(createRollout).toHaveBeenCalled());
    const body = createRollout.mock.calls[0][0] as Record<string, unknown>;
    // instances = 勾选实例的 containerId 列表。
    expect(body.instances).toEqual(['cid-1']);
  });

  it('提交成功 → 切进度阶段:轮询 getRollout(rolloutId) 并内嵌逐实例进度;终态(done)停轮询', async () => {
    // 第一次回 running(逐实例 in-progress),之后回 done —— 验证轮询驱动了第二次拉取与状态更新。
    getRollout
      .mockResolvedValueOnce(detailOf('running'))
      .mockResolvedValue(detailOf('done'));
    const onSubmitted = vi.fn();
    const user = userEvent.setup();
    renderModal({ onSubmitted });
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    // 提交回调通知刷新(传 rolloutId)。
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('ro-1'));

    // 切进度阶段:标题变「投放进度」,立即按 rolloutId 拉详情。
    expect(await screen.findByText('投放进度')).toBeInTheDocument();
    await waitFor(() => expect(getRollout).toHaveBeenCalledWith('ro-1'));

    // 轮询驱动:running → done(getRollout 被多次调用),最终显「投放完成」绿 Alert。
    expect(await screen.findByText('投放完成')).toBeInTheDocument();
    await waitFor(() => expect(getRollout.mock.calls.length).toBeGreaterThanOrEqual(2));

    // 逐实例进度内嵌(done 节点)。
    expect(screen.getByText('10.0.0.1:8080')).toBeInTheDocument();

    // 终态后停轮询:再等一会调用次数不再无界增长(取一个快照,稍后应不变)。
    const callsAtTerminal = getRollout.mock.calls.length;
    await new Promise((r) => setTimeout(r, 80)); // 4 个轮询周期(20ms)
    expect(getRollout.mock.calls.length).toBe(callsAtTerminal);
  });

  it('失败终态(failed + frozen):进度区红 Alert 引导去记录页处置', async () => {
    getRollout.mockResolvedValue(detailOf('failed', { frozen: true, mode: 'pull-redeploy' }));
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    await user.click(screen.getByText(byNormalizedName('重拉镜像(镜像变更)')));
    await waitFor(() =>
      expect(screen.getAllByDisplayValue('oci.example.com/app:1.7.20').length).toBeGreaterThanOrEqual(1),
    );
    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    expect(await screen.findByText('投放失败已冻结')).toBeInTheDocument();
    // pull-redeploy 失败:引导重新发起带镜像的投放。
    expect(
      screen.getByText((t) => t.includes('pull-redeploy 需重新发起一次带镜像的投放')),
    ).toBeInTheDocument();
  });

  it('409 同服务投放进行中 → 精确提示,不切进度阶段', async () => {
    createRollout.mockRejectedValue({ response: { status: 409, data: { detail: '同服务投放进行中' } } });
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => expect(createRollout).toHaveBeenCalled());
    // 精确提示(后端 detail)。
    expect(await screen.findByText('同服务投放进行中')).toBeInTheDocument();
    // 仍在配置阶段(未切进度:配置说明仍在、无进度标题,不轮询)。
    expect(screen.getByText('按你的变更类型选择投放机制')).toBeInTheDocument();
    expect(screen.queryByText('投放进度')).not.toBeInTheDocument();
    expect(getRollout).not.toHaveBeenCalled();
  });

  it('422 缺 image / 非法 mode → 精确提示(后端 detail),不切进度阶段', async () => {
    createRollout.mockRejectedValue({
      response: { status: 422, data: { detail: 'pull-redeploy 投放须指定 image' } },
    });
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => expect(createRollout).toHaveBeenCalled());
    expect(await screen.findByText('pull-redeploy 投放须指定 image')).toBeInTheDocument();
    expect(getRollout).not.toHaveBeenCalled();
  });

  it('无 nacosServiceName 的服务在下拉中禁选(不可投放)', async () => {
    const user = userEvent.setup();
    renderModal();
    await screen.findByText('按你的变更类型选择投放机制');
    const combobox = await screen.findByRole('combobox');
    await user.click(combobox);
    // svc-nonacos 选项带「不可投放」标注且为 disabled 项。
    const disabledOpt = await screen.findByText(
      (_t, node) =>
        node?.classList.contains('ant-select-item-option-content') === true &&
        node.textContent?.includes('不可投放') === true,
    );
    expect(disabledOpt).toBeInTheDocument();
    const optionEl = disabledOpt.closest('.ant-select-item-option');
    expect(optionEl?.className).toContain('ant-select-item-option-disabled');
  });

  it('关弹窗清轮询定时器:进度阶段点关闭 → onClose 触发,后续不再轮询', async () => {
    getRollout.mockResolvedValue(detailOf('running')); // 保持 running 让轮询持续
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderModal({ onClose });
    await screen.findByText('按你的变更类型选择投放机制');
    await selectService(user, 'svc-demo');
    const submit = screen.getByRole('button', { name: byNormalizedName('发起投放') });
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    // 进入进度阶段并至少轮询过。
    await screen.findByText('投放进度');
    await waitFor(() => expect(getRollout).toHaveBeenCalled());

    // 点关闭(running 态按钮文案「关闭(后台继续)」)。
    await user.click(screen.getByRole('button', { name: byNormalizedName('关闭') }));
    expect(onClose).toHaveBeenCalled();

    // 关闭后清了定时器:快照调用数,稍后不再增长。
    const callsAtClose = getRollout.mock.calls.length;
    await new Promise((r) => setTimeout(r, 80));
    expect(getRollout.mock.calls.length).toBe(callsAtClose);
  });

  it('全局命名空间收窄服务下拉:选了具体 ns 仅列该 ns 的服务', async () => {
    // 多一个别的 ns 的服务,验证被收窄掉。
    list.mockImplementation((resource: string) =>
      Promise.resolve(
        resource === 'services'
          ? envelope([
              ...servicesEnvelope.rows,
              { id: 50, serviceCode: 'svc-prod', namespaceId: 9, namespaceCode: 'ns-prod', nacosServiceName: 'wms-prod' },
            ])
          : envelope([]),
      ),
    );
    const user = userEvent.setup();
    renderModal({}, { id: 1, code: 'ns-admin' });
    await screen.findByText('按你的变更类型选择投放机制');
    const combobox = await screen.findByRole('combobox');
    await user.click(combobox);
    await waitFor(() => {
      const opts = Array.from(document.querySelectorAll('.ant-select-item-option-content')).map(
        (o) => o.textContent,
      );
      // ns-admin 的服务在,ns-prod(id=9)的不在。
      expect(opts.some((t) => t?.includes('svc-demo'))).toBe(true);
      expect(opts.some((t) => t?.includes('svc-prod'))).toBe(false);
    });
  });
});
