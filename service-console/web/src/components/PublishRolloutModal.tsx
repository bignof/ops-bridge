import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  AutoComplete,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Radio,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import * as resources from '../api/resources';
import type {
  InstanceRow,
  RolloutDetail,
  RolloutStatus,
  ServiceImageRow,
} from '../api/resources';
import { useNamespace } from '../context/NamespaceContext';
import RollingTaskNodesTable from './RollingTaskNodesTable';

const { Text } = Typography;

// 服务下拉项(对齐 ServicesPage 的 ServiceRow 子集)。投放须取 nacosServiceName 作 POST serviceName;
// id 给镜像台账(listServiceImages),namespaceCode/nacosServiceName 给灰度候选过滤,namespaceId 给全局 ns 收窄。
interface ServiceOption {
  id: string | number;
  serviceCode: string;
  namespaceId?: string | number;
  namespaceCode?: string;
  /** Nacos 服务名(滚动部署寻址用);== POST /api/rollouts 的 serviceName。无则不可投放。 */
  nacosServiceName?: string;
}

// 各 list 端点硬卡 pageSize le=200(后端 Query le=200,前后端必须一致;>200 会 422)。
const OPTIONS_PAGE_SIZE = 200;

// 投放模式:对照后端 RolloutCreateIn.mode(restart | pull-redeploy)。
type RolloutMode = 'restart' | 'pull-redeploy';
// 投放范围:全部实例 / 灰度子集(后者多选 containerId 作 instances)。
type RolloutScope = 'all' | 'canary';

// 投放进度终态(done/degraded/failed):进入即停轮询。running 继续轮询。
const isTerminal = (status: RolloutStatus | string): boolean =>
  status === 'done' || status === 'degraded' || status === 'failed';

// 投放状态 → Tag(与 RolloutsPage renderStatusTag 同口径:running 蓝 / done 绿 / degraded 黄 / failed 红)。
const renderStatusTag = (status: RolloutStatus | string) => {
  switch (status) {
    case 'running':
      return <Tag color="blue">进行中</Tag>;
    case 'done':
      return <Tag color="green">完成</Tag>;
    case 'degraded':
      return <Tag color="gold">降级完成</Tag>;
    case 'failed':
      return <Tag color="red">失败</Tag>;
    default:
      return <Tag>{status}</Tag>;
  }
};

export interface PublishRolloutModalProps {
  open: boolean;
  /** 关闭弹窗(配置阶段取消 / 进度阶段关闭都走它)。调用方据此置 open=false。 */
  onClose: () => void;
  /**
   * 投放成功提交后回调(传 rolloutId)。调用方据此刷新投放记录列表(RolloutsPage actionRef.reload)。
   * 注:这是「已发起」回调,不等于「已完成」;完成态在弹窗进度区实时展示。
   */
  onSubmitted?: (rolloutId: string) => void;
  /**
   * 轮询进度的间隔(ms,默认 2000)。抽出为 prop 便于测试注入小间隔 + vi.useFakeTimers 驱动轮询,
   * 不写死不可控的定时器。
   */
  pollIntervalMs?: number;
}

/**
 * 发布投放弹窗(P4-5,desired-state 投放闭环主菜)。
 *
 * 让运维对某服务发起一次投放(把 desired-state 推到运行实例),分两阶段:
 *  1. **配置**:选服务 → 选机制(重启 / 重拉镜像)→ 选范围(全部 / 灰度选实例)→ force → 投放摘要 →
 *     提交 createRollout 组装 body。422(缺 image / 非法 mode)/ 409(同服务投放进行中)本地精确提示。
 *  2. **进度**:提交成功后轮询 getRollout(rolloutId),内嵌 {@link RollingTaskNodesTable} 看逐实例实时进度;
 *     进入终态(done/degraded/failed)即停轮询、显结果;failed+frozen 红 Alert 引导下一步。
 *
 * 设计取向(用户要:人性化、易用、闭环):机制由用户**按变更类型手选**(本期不假装按 diff 自动判定);
 * pull-redeploy 的 image 后端不落库,故默认把镜像 tag 兜底填进 target 便于记录页识别。
 *
 * 入口:RolloutsPage 工具栏「发起投放」。提交成功后经 onSubmitted 刷新记录页列表;关弹窗清轮询定时器。
 */
export default function PublishRolloutModal({
  open,
  onClose,
  onSubmitted,
  pollIntervalMs = 2000,
}: PublishRolloutModalProps) {
  const [messageApi, contextHolder] = message.useMessage();
  // P3-10:全局命名空间(选具体 ns 时收窄服务下拉)。
  const { namespace } = useNamespace();

  // ── 阶段:'config' 配置中 / 'progress' 已提交看进度 ──────────────────────────────
  const [phase, setPhase] = useState<'config' | 'progress'>('config');

  // ── 配置:服务下拉 ──────────────────────────────────────────────────────────────
  const [serviceOptions, setServiceOptions] = useState<ServiceOption[]>([]);
  const [svcLoading, setSvcLoading] = useState(false);
  const [svcErrored, setSvcErrored] = useState(false);
  const [serviceId, setServiceId] = useState<string | number | undefined>(undefined);

  // ── 配置:机制 / 镜像 / 范围 / force / 摘要 ───────────────────────────────────────
  const [mode, setMode] = useState<RolloutMode>('restart');
  const [image, setImage] = useState('');
  const [scope, setScope] = useState<RolloutScope>('all');
  const [selectedInstances, setSelectedInstances] = useState<string[]>([]);
  const [force, setForce] = useState(false);
  const [target, setTarget] = useState('');
  // 用户是否手改过 target:改过则不再被「默认填镜像 tag」逻辑覆盖(尊重手输)。
  const targetTouchedRef = useRef(false);

  // ── 配置:选定服务的镜像台账(pull-redeploy 预填 + 历史下拉)──────────────────────
  const [serviceImages, setServiceImages] = useState<ServiceImageRow[]>([]);
  const [imagesLoading, setImagesLoading] = useState(false);

  // ── 配置:灰度候选实例(按服务 nacosServiceName 过滤)────────────────────────────
  const [candidateInstances, setCandidateInstances] = useState<InstanceRow[]>([]);
  const [instancesLoading, setInstancesLoading] = useState(false);
  const [instancesErrored, setInstancesErrored] = useState(false);

  // 提交中(防重复点击 + 按钮 loading)。
  const [submitting, setSubmitting] = useState(false);

  // ── 进度:轮询 getRollout(rolloutId)─────────────────────────────────────────────
  const [rolloutId, setRolloutId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RolloutDetail | null>(null);
  const [detailErrored, setDetailErrored] = useState(false);
  // 轮询定时器句柄(关弹窗 / 进入终态时清,避免泄漏)。
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 选中服务的展示行(取 nacosServiceName / namespaceCode 等)。
  const selectedService = useMemo(
    () => serviceOptions.find((s) => String(s.id) === String(serviceId)),
    [serviceOptions, serviceId],
  );

  // 全局 ns 收窄后的服务全集(选了具体 ns → 仅该 ns 的服务;「全部」→ 全列)。
  const visibleServices = useMemo(
    () =>
      namespace === null
        ? serviceOptions
        : serviceOptions.filter((s) => String(s.namespaceId) === String(namespace.id)),
    [serviceOptions, namespace],
  );

  // 服务下拉 options(label=namespaceCode/serviceCode,value=id;无 nacosServiceName 的禁选并标注)。
  const serviceSelectOptions = useMemo(
    () =>
      visibleServices.map((s) => ({
        label: s.nacosServiceName
          ? s.namespaceCode
            ? `${s.namespaceCode}/${s.serviceCode}`
            : s.serviceCode
          : `${s.namespaceCode ? `${s.namespaceCode}/` : ''}${s.serviceCode}(未配 Nacos 服务名,不可投放)`,
        value: s.id,
        disabled: !s.nacosServiceName,
      })),
    [visibleServices],
  );

  // 镜像历史下拉 options(pull-redeploy 选历史镜像;AutoComplete 既可选历史也可手填)。
  const imageOptions = useMemo(
    () => serviceImages.map((img) => ({ value: img.image })),
    [serviceImages],
  );

  // 拉服务列表(配置阶段进入即拉一次)。失败置错误态,给重试。
  const fetchServices = useCallback(async () => {
    setSvcLoading(true);
    setSvcErrored(false);
    try {
      const env = await resources.list<ServiceOption>('services', { pageSize: OPTIONS_PAGE_SIZE });
      setServiceOptions(env.rows);
    } catch {
      setSvcErrored(true);
      setServiceOptions([]);
    } finally {
      setSvcLoading(false);
    }
  }, []);

  // 弹窗打开时拉服务列表(每次打开都刷新,确保新建服务可见)。
  useEffect(() => {
    if (open) void fetchServices();
  }, [open, fetchServices]);

  // 拉选定服务的镜像台账(pull-redeploy 预填当前镜像 + 历史下拉)。失败静默(全局兜底 toast 已弹;
  // 镜像非空校验在提交门拦,这里只是预填便利)。
  const fetchImages = useCallback(async (svcId: string | number) => {
    setImagesLoading(true);
    try {
      const env = await resources.listServiceImages<ServiceImageRow>(svcId);
      setServiceImages(env.rows);
      // 预填当前镜像(isCurrent),仅当用户尚未手填时填(尊重已输入值)。
      const current = env.rows.find((r) => r.isCurrent);
      if (current) {
        setImage((prev) => (prev ? prev : current.image));
      }
    } catch {
      setServiceImages([]);
    } finally {
      setImagesLoading(false);
    }
  }, []);

  // 拉灰度候选实例(按服务 nacosServiceName 前端过滤;后端按 namespace=agentId 收窄候选量)。
  // 用 namespaceCode(= 发现侧 agentId)作 namespace 参数;失败置错误态。
  const fetchCandidateInstances = useCallback(async (svc: ServiceOption) => {
    setInstancesLoading(true);
    setInstancesErrored(false);
    try {
      const params: resources.ListParams = { page: 1, pageSize: OPTIONS_PAGE_SIZE };
      // 该服务有命名空间 code 则按它收窄发现实例(agentId == 命名空间 code)。
      if (svc.namespaceCode) params.namespace = svc.namespaceCode;
      const env = await resources.listInstances<InstanceRow>(params);
      // 前端再按 nacosService == 该服务的 nacosServiceName 精确过滤(同 ns 下可能有多服务)。
      const matched = env.rows.filter((r) => r.nacosService === svc.nacosServiceName);
      setCandidateInstances(matched);
    } catch {
      setInstancesErrored(true);
      setCandidateInstances([]);
    } finally {
      setInstancesLoading(false);
    }
  }, []);

  // 选定服务变化:拉镜像台账;清空灰度选择(换服务后旧实例不适用)。
  useEffect(() => {
    if (serviceId === undefined) {
      setServiceImages([]);
      return;
    }
    setSelectedInstances([]);
    void fetchImages(serviceId);
  }, [serviceId, fetchImages]);

  // 切到灰度范围且已选服务:拉该服务候选实例(供多选)。
  useEffect(() => {
    if (scope === 'canary' && selectedService?.nacosServiceName) {
      void fetchCandidateInstances(selectedService);
    }
  }, [scope, selectedService, fetchCandidateInstances]);

  // pull-redeploy 时把镜像 tag 兜底填进 target(image 后端不落库,审计兜底);用户手改过则不覆盖。
  // 切回 restart 时,若 target 仍是自动填的镜像值则清掉(避免 restart 带无关镜像摘要)。
  useEffect(() => {
    if (targetTouchedRef.current) return;
    if (mode === 'pull-redeploy') {
      setTarget(image);
    } else {
      setTarget('');
    }
  }, [mode, image]);

  // 全局 ns 切换后,若当前选中服务已不在收窄集合里,清空选中(避免展示与下拉不一致)。
  useEffect(() => {
    if (serviceId === undefined) return;
    if (!visibleServices.some((s) => String(s.id) === String(serviceId))) {
      setServiceId(undefined);
    }
  }, [visibleServices, serviceId]);

  // 清轮询定时器(进入终态 / 关弹窗 / 卸载都调)。
  const clearPoll = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // 拉一次详情(进度阶段轮询体);进入终态即停轮询。失败置错误态(进度阶段不退弹窗,给重试)。
  const pollOnce = useCallback(
    async (id: string) => {
      try {
        const d = await resources.getRollout(id);
        setDetail(d);
        setDetailErrored(false);
        if (isTerminal(d.status)) clearPoll();
      } catch {
        setDetailErrored(true);
      }
    },
    [clearPoll],
  );

  // 卸载兜底:组件销毁时清定时器(避免泄漏)。
  useEffect(() => clearPoll, [clearPoll]);

  // 重置所有配置 + 进度态(关弹窗 / 再次发起时回到干净配置阶段)。
  const resetAll = useCallback(() => {
    clearPoll();
    setPhase('config');
    setServiceId(undefined);
    setMode('restart');
    setImage('');
    setScope('all');
    setSelectedInstances([]);
    setForce(false);
    setTarget('');
    targetTouchedRef.current = false;
    setServiceImages([]);
    setCandidateInstances([]);
    setInstancesErrored(false);
    setRolloutId(null);
    setDetail(null);
    setDetailErrored(false);
    setSubmitting(false);
  }, [clearPoll]);

  // 关弹窗:清定时器 + 回调通知调用方刷新(若已提交过)+ 重置态。
  const handleClose = useCallback(() => {
    clearPoll();
    onClose();
  }, [clearPoll, onClose]);

  // 弹窗从开 → 关:重置态(下次打开是干净配置阶段)。用 afterClose 在动画结束后重置避免闪烁。
  const handleAfterClose = useCallback(() => {
    resetAll();
  }, [resetAll]);

  // 提交门:服务必选(且有 nacosServiceName);pull-redeploy 须有 image;灰度须至少选 1 个实例。
  const canSubmit = useMemo(() => {
    if (!selectedService?.nacosServiceName) return false;
    if (mode === 'pull-redeploy' && image.trim() === '') return false;
    if (scope === 'canary' && selectedInstances.length === 0) return false;
    return true;
  }, [selectedService, mode, image, scope, selectedInstances]);

  // 提交:组装 body → createRollout → 切进度阶段并立即拉一次 + 启轮询。
  // 422/409 本地精确提示(suppressGlobalError);其余失败 generic fallback,不静默吞。
  const handleSubmit = async () => {
    const svc = selectedService;
    if (!svc?.nacosServiceName) return;
    const trimmedImage = image.trim();
    const trimmedTarget = target.trim();

    // 组装 body(对照后端 RolloutCreateIn):
    //  - serviceName = nacosServiceName(必填,后端寻址 + 抢锁)。
    //  - namespace = 该服务命名空间 code(审计 + 列表过滤)。
    //  - mode = 机制;image 仅 pull-redeploy 带。
    //  - instances = 灰度子集(containerId);全部范围不传 = 全量。
    //  - force / target 直传(target 为空则不带)。
    const body: resources.CreateRolloutParams = {
      serviceName: svc.nacosServiceName,
      mode,
      force,
    };
    if (svc.namespaceCode) body.namespace = svc.namespaceCode;
    if (mode === 'pull-redeploy') body.image = trimmedImage;
    if (scope === 'canary' && selectedInstances.length > 0) body.instances = selectedInstances;
    if (trimmedTarget !== '') body.target = trimmedTarget;

    setSubmitting(true);
    try {
      const out = await resources.createRollout(body);
      messageApi.success(`已发起投放(rolloutId=${out.rolloutId})`);
      onSubmitted?.(out.rolloutId);
      // 切进度阶段:记 rolloutId,立即拉一次详情,再按间隔轮询直到终态。
      setRolloutId(out.rolloutId);
      setPhase('progress');
      setDetail(null);
      setDetailErrored(false);
      void pollOnce(out.rolloutId);
      clearPoll();
      pollTimerRef.current = setInterval(() => {
        void pollOnce(out.rolloutId);
      }, pollIntervalMs);
    } catch (e) {
      // 后端 detail 精确提示:422(缺 image / 非法 mode)、409(同服务投放进行中);其余 generic。
      const err = e as { response?: { status?: number; data?: { detail?: unknown } } };
      const httpStatus = err.response?.status;
      const detailMsg = err.response?.data?.detail;
      if (httpStatus === 409) {
        messageApi.error(
          typeof detailMsg === 'string' && detailMsg.trim()
            ? detailMsg
            : '该服务已有投放进行中,请稍后或去记录页查看',
        );
      } else if (httpStatus === 422) {
        messageApi.error(
          typeof detailMsg === 'string' && detailMsg.trim() ? detailMsg : '投放参数不合法,请检查机制与镜像',
        );
      } else {
        // 非预期失败:不静默吞(A2),给 generic 兜底(opt-out 了全局 toast)。
        messageApi.error(
          typeof detailMsg === 'string' && detailMsg.trim() ? detailMsg : '发起投放失败,请稍后重试',
        );
      }
    } finally {
      setSubmitting(false);
    }
  };

  // 灰度候选实例多选表的列:容器名 / 地址(agentId)/ 健康。rowKey = containerId(POST instances 即它)。
  const instanceColumns: ColumnsType<InstanceRow> = [
    { title: '容器', dataIndex: 'containerName', key: 'containerName' },
    {
      title: '地址(机器)',
      dataIndex: 'agentId',
      key: 'agentId',
      render: (_v, r) => <Text code>{r.agentId}</Text>,
    },
    {
      title: '健康',
      dataIndex: 'healthy',
      key: 'healthy',
      width: 90,
      render: (_v, r) =>
        r.healthy === null || r.healthy === undefined ? (
          '-'
        ) : r.healthy ? (
          <Tag color="green">健康</Tag>
        ) : (
          <Tag color="gold">不健康</Tag>
        ),
    },
    {
      title: '运行',
      dataIndex: 'running',
      key: 'running',
      width: 90,
      render: (_v, r) => (r.running ? <Tag color="green">运行中</Tag> : <Tag color="gold">已停</Tag>),
    },
  ];

  // 配置阶段表单。
  const renderConfig = () => {
    if (svcLoading) {
      return (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
          <Spin />
        </div>
      );
    }
    if (svcErrored) {
      return (
        <Alert
          type="error"
          showIcon
          message="加载服务列表失败"
          description="请检查网络或稍后重试。"
          action={
            <Button size="small" onClick={() => void fetchServices()}>
              重试
            </Button>
          }
        />
      );
    }
    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {/* 顶部说明:本期机制按变更类型手选(不假装按 diff 自动算)。 */}
        <Alert
          type="info"
          showIcon
          message="按你的变更类型选择投放机制"
          description="仅改了插件 → 选「重启」;改了镜像 → 选「重拉镜像」并指定镜像。系统本期不自动比对差异,由你判断。"
        />

        <Form layout="vertical">
          {/* 1. 服务 */}
          <Form.Item label="服务" required style={{ marginBottom: 16 }}>
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              style={{ width: '100%' }}
              placeholder="请选择要投放的服务"
              options={serviceSelectOptions}
              value={serviceId}
              onChange={(v) => setServiceId(v)}
              notFoundContent={visibleServices.length ? undefined : '当前命名空间下暂无服务'}
            />
            {selectedService?.nacosServiceName ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                Nacos 服务名:<Text code>{selectedService.nacosServiceName}</Text>
              </Text>
            ) : null}
          </Form.Item>

          {/* 2. 机制 */}
          <Form.Item label="机制" required style={{ marginBottom: 16 }}>
            <Radio.Group value={mode} onChange={(e) => setMode(e.target.value as RolloutMode)}>
              <Space direction="vertical">
                <Radio value="restart">
                  重启(插件变更) ——{' '}
                  <Text type="secondary">worker 优雅重启时从 agent 缓存重新拉取插件</Text>
                </Radio>
                <Radio value="pull-redeploy">
                  重拉镜像(镜像变更) ——{' '}
                  <Text type="secondary">逐实例优雅重拉指定镜像并重建容器</Text>
                </Radio>
              </Space>
            </Radio.Group>
          </Form.Item>

          {/* 2b. 镜像(仅 pull-redeploy) */}
          {mode === 'pull-redeploy' ? (
            <Form.Item
              label="镜像"
              required
              style={{ marginBottom: 16 }}
              validateStatus={image.trim() === '' ? 'error' : undefined}
              help={image.trim() === '' ? '重拉镜像必须指定镜像(否则无法投放)' : undefined}
            >
              <AutoComplete
                style={{ width: '100%' }}
                options={imageOptions}
                value={image}
                onChange={(v) => setImage(v)}
                placeholder={
                  imagesLoading ? '正在加载镜像台账…' : '默认预填当前镜像,可选历史镜像或手填'
                }
                filterOption={(input, option) =>
                  (option?.value ?? '').toLowerCase().includes(input.toLowerCase())
                }
              >
                <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} />
              </AutoComplete>
              {imagesLoading ? null : serviceImages.length === 0 ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  该服务镜像台账为空 —— 请手填目标镜像。
                </Text>
              ) : null}
            </Form.Item>
          ) : null}

          {/* 3. 范围 */}
          <Form.Item label="范围" required style={{ marginBottom: 16 }}>
            <Radio.Group value={scope} onChange={(e) => setScope(e.target.value as RolloutScope)}>
              <Radio value="all">全部实例</Radio>
              <Radio value="canary">灰度(选实例)</Radio>
            </Radio.Group>
            {scope === 'canary' ? (
              <div style={{ marginTop: 12 }}>
                {!selectedService ? (
                  <Text type="secondary">请先选择服务以列出其实例。</Text>
                ) : instancesErrored ? (
                  <Alert
                    type="error"
                    showIcon
                    message="加载候选实例失败"
                    description="请稍后重试。"
                    action={
                      <Button
                        size="small"
                        onClick={() => {
                          if (selectedService?.nacosServiceName)
                            void fetchCandidateInstances(selectedService);
                        }}
                      >
                        重试
                      </Button>
                    }
                  />
                ) : (
                  <>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      只滚所选实例(灰度);集群健康门仍按全集判定(灰度 1 个也需全集健康 ≥2 或勾选强制)。
                    </Text>
                    <Table<InstanceRow>
                      style={{ marginTop: 8 }}
                      rowKey={(r) => r.containerId ?? `${r.agentId}:${r.containerName}`}
                      columns={instanceColumns}
                      dataSource={candidateInstances}
                      loading={instancesLoading}
                      pagination={false}
                      size="small"
                      // 仅有 containerId 的实例可被选(POST instances 是 containerId 列表)。
                      rowSelection={{
                        selectedRowKeys: selectedInstances,
                        onChange: (keys) => setSelectedInstances(keys as string[]),
                        getCheckboxProps: (r) => ({ disabled: !r.containerId }),
                      }}
                      locale={{ emptyText: '该服务暂无可选实例' }}
                    />
                    {selectedInstances.length === 0 ? (
                      <Text type="danger" style={{ fontSize: 12 }}>
                        请至少选择一个实例。
                      </Text>
                    ) : null}
                  </>
                )}
              </div>
            ) : null}
          </Form.Item>

          {/* 4. force */}
          <Form.Item style={{ marginBottom: 16 }}>
            <Checkbox checked={force} onChange={(e) => setForce(e.target.checked)}>
              强制(集群健康实例 &lt; 2 仍滚,可能瞬时中断)
            </Checkbox>
          </Form.Item>

          {/* 5. 投放摘要 target(审计;pull-redeploy 默认填镜像 tag) */}
          <Form.Item
            label="投放摘要(可选,审计用)"
            style={{ marginBottom: 0 }}
            help={
              mode === 'pull-redeploy'
                ? '镜像不落投放记录,默认以镜像 tag 作摘要便于记录页识别;可改。'
                : undefined
            }
          >
            <Input
              placeholder="本次投放的人读摘要"
              value={target}
              onChange={(e) => {
                targetTouchedRef.current = true;
                setTarget(e.target.value);
              }}
            />
          </Form.Item>
        </Form>
      </Space>
    );
  };

  // 进度阶段:轮询 getRollout 展示 status + frozen + 逐实例进度。
  const renderProgress = () => {
    const status = detail?.status;
    const frozen = detail?.frozen;
    const terminal = status ? isTerminal(status) : false;
    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {/* 顶部状态条:status Tag + 服务名;running 时显「轮询中」。 */}
        <Space wrap>
          <Text strong>投放状态:</Text>
          {status ? renderStatusTag(status) : <Tag color="blue">进行中</Tag>}
          {detail?.serviceName ? <Tag color="blue">{detail.serviceName}</Tag> : null}
          {!terminal ? <Text type="secondary">(实时刷新中…)</Text> : null}
        </Space>

        {/* failed + frozen:红 Alert 引导下一步处置。 */}
        {status === 'failed' && frozen ? (
          <Alert
            type="error"
            showIcon
            message="投放失败已冻结"
            description={
              detail?.mode === 'pull-redeploy'
                ? '本次投放失败并冻结,部分实例可能已切到新镜像。请到「投放记录」页处置;pull-redeploy 需重新发起一次带镜像的投放。'
                : '本次投放失败并冻结,部分实例可能已切到新目标。请到「投放记录」页重试或回滚。'
            }
          />
        ) : null}

        {/* degraded:黄 Alert(降级完成,部分实例跳过)。 */}
        {status === 'degraded' ? (
          <Alert
            type="warning"
            showIcon
            message="降级完成"
            description="集群健康实例不足,部分实例被跳过;投放未全量推完,请到「投放记录」页核对。"
          />
        ) : null}

        {/* done:绿 Alert(全量完成)。 */}
        {status === 'done' ? (
          <Alert type="success" showIcon message="投放完成" description="所有目标实例已滚动完成。" />
        ) : null}

        {/* 详情拉取失败(轮询中网络抖动):提示,不退弹窗(下个 tick 会重试,也可手动立即重试)。 */}
        {detailErrored && !terminal ? (
          <Alert
            type="warning"
            showIcon
            message="进度刷新失败,正在重试…"
            action={
              rolloutId ? (
                <Button size="small" onClick={() => void pollOnce(rolloutId)}>
                  立即刷新
                </Button>
              ) : null
            }
          />
        ) : null}

        {/* 逐实例进度(复用组件;null/空 → 组件内占位「暂无滚动进度」)。 */}
        <div>
          <Text strong>逐实例进度</Text>
          <div style={{ marginTop: 8 }}>
            {detail === null && !detailErrored ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: 24 }}>
                <Spin />
              </div>
            ) : (
              <RollingTaskNodesTable task={detail?.rollingTask ?? null} />
            )}
          </div>
        </div>
      </Space>
    );
  };

  // 底部按钮:配置阶段 = 取消 / 发起投放;进度阶段 = 关闭(终态)或 后台运行(running,允许关弹窗继续后台跑)。
  const footer =
    phase === 'config'
      ? [
          <Button key="cancel" onClick={handleClose}>
            取消
          </Button>,
          <Button
            key="submit"
            type="primary"
            loading={submitting}
            disabled={!canSubmit}
            onClick={() => void handleSubmit()}
          >
            发起投放
          </Button>,
        ]
      : [
          <Button key="close" type="primary" onClick={handleClose}>
            {detail && isTerminal(detail.status) ? '关闭' : '关闭(后台继续)'}
          </Button>,
        ];

  return (
    <>
      {contextHolder}
      <Modal
        title={phase === 'config' ? '发起投放' : '投放进度'}
        open={open}
        onCancel={handleClose}
        afterClose={handleAfterClose}
        footer={footer}
        width={760}
        maskClosable={false}
        destroyOnClose={false}
      >
        {phase === 'config' ? renderConfig() : renderProgress()}
      </Modal>
    </>
  );
}
