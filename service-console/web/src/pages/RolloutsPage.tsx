import { useCallback, useEffect, useRef, useState } from 'react';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import * as resources from '../api/resources';
import type { RolloutDetail, RolloutRow, RolloutStatus } from '../api/resources';
import { useNamespace } from '../context/NamespaceContext';
import RollingTaskNodesTable from '../components/RollingTaskNodesTable';

const { Text } = Typography;

// 投放状态 → Tag(对照 brief:running 蓝 / done 绿 / degraded 黄 / failed 红)。未知值原样显示。
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

// 触发来源中文化(manual/publish/retry/rollback);未知值原样显示。
const TRIGGER_TEXT: Record<string, string> = {
  manual: '手动',
  publish: '发布',
  retry: '重试',
  rollback: '回滚',
};
const renderTrigger = (trigger: string) => TRIGGER_TEXT[trigger] ?? trigger;

// 可空文本统一渲染:null/undefined/空串 → 「-」。
const dash = (v?: string | null) => (v ? v : '-');

// status 筛选下拉枚举(valueEnum 让 ProTable 查询表单渲染为本地 Select,无需远程 request)。
const STATUS_VALUE_ENUM = {
  running: { text: '进行中' },
  done: { text: '完成' },
  degraded: { text: '降级完成' },
  failed: { text: '失败' },
};

// 失败处置按钮的可见性(避免点了必 409):
//  - 重试:仅 status=failed 可。
//  - 回滚:仅 status=failed 且 previousTarget 非空 可(后端无上一版会 409)。
const canRetry = (r: Pick<RolloutRow, 'status'>) => r.status === 'failed';
const canRollback = (r: Pick<RolloutRow, 'status' | 'previousTarget'>) =>
  r.status === 'failed' && !!r.previousTarget;

/**
 * 投放记录页(resource `rollouts`):投放运行记录 + 逐实例进度 + 失败处置(重试/回滚)。
 *
 * 一次投放 = 一条 rollout 记录(运行态)+ 一条底层 rolling task(逐实例进度),二者经
 * rollingTaskId 关联。本页只做「记录列表 + 详情进度 + 失败重试/回滚」;**发起投放**(POST /api/rollouts)
 * 属 P4-5 发布弹窗,本页不做。
 *
 * 列表:服务端分页(ProTable `request` 映射 current/pageSize → 后端 page/pageSize,读统一信封)。
 * 筛选:status(下拉)、serviceName(文本)。
 * P3-10 顶栏命名空间切换器联动:选具体 ns → 以其 code 作 namespace 过滤(覆盖本页无 ns 筛选列,经
 *       `params` 感知变更自动重拉);选「全部」→ 不传 namespace。
 *
 * 行操作「详情」:打开 Drawer 拉 getRollout(id),展示 RolloutOut 字段 + rollingTask.nodes 逐实例进度
 *       小表({@link RollingTaskNodesTable},把「投放 → 底层滚动逐实例」串起来,一眼看到卡在哪个实例)。
 *       frozen 时 Drawer 顶 Alert 标「失败已冻结(半迁移态),可重试或回滚」。
 *
 * 失败处置(行内 + 详情 Drawer 内):
 *  - 重试:仅 failed 显示 → Popconfirm → retryRollout → 成功 toast(含 rolloutId)+ 刷新。
 *  - 回滚:仅 failed 且 previousTarget 非空 显示 → Popconfirm → rollbackRollout → 成功 toast + 刷新。
 *  - retry/rollback 失败(如 409)走 client 全局兜底 toast(本页不 opt-out,统一兜底,不静默吞)。
 */
export default function RolloutsPage() {
  const [messageApi, contextHolder] = message.useMessage();
  const actionRef = useRef<ActionType>();

  // P3-10:全局命名空间选中态(null = 全部命名空间)。选具体 ns 时按其 code 强制过滤。
  const { namespace } = useNamespace();

  // 详情 Drawer:当前查看的投放行(为空=关闭)+ 拉取的详情(含 rollingTask)+ 加载/错误态。
  const [detailRow, setDetailRow] = useState<RolloutRow | null>(null);
  const [detail, setDetail] = useState<RolloutDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErrored, setDetailErrored] = useState(false);

  // 失败处置提交中的目标(行 id + 动作),用于按钮 loading + 防重复点击。
  const [actingId, setActingId] = useState<string | null>(null);

  const reload = () => actionRef.current?.reload();

  // 全局 ns 变更时重拉列表(ProTable params 变更会自动触发,这里 actionRef 兜底显式 reload)。
  useEffect(() => {
    actionRef.current?.reload();
  }, [namespace]);

  // 拉详情(含 rollingTask 逐实例进度);失败置错误态(全局兜底 toast 已由拦截器弹,本地给重试)。
  const fetchDetail = useCallback(async (id: string) => {
    setDetailLoading(true);
    setDetailErrored(false);
    try {
      const d = await resources.getRollout(id);
      setDetail(d);
    } catch {
      setDetailErrored(true);
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const openDetail = (row: RolloutRow) => {
    setDetailRow(row);
    setDetail(null);
    void fetchDetail(row.id);
  };

  const closeDetail = () => {
    setDetailRow(null);
    setDetail(null);
    setDetailErrored(false);
  };

  // 重试:retryRollout(id) → 成功 toast(含新 rolloutId)+ 关抽屉(若开)+ 刷新列表。
  // 失败由 client 拦截器统一兜底 toast(本页不 opt-out);仅复位 loading,不崩溃。
  const handleRetry = async (row: Pick<RolloutRow, 'id'>) => {
    setActingId(row.id);
    try {
      const out = await resources.retryRollout(row.id);
      messageApi.success(`已发起重试(rolloutId=${out.rolloutId})`);
      if (detailRow?.id === row.id) closeDetail();
      reload();
    } catch {
      // 失败由 client 拦截器统一提示(含后端 detail,如 409);此处不额外处理。
    } finally {
      setActingId(null);
    }
  };

  // 回滚:rollbackRollout(id) → 成功 toast(含新 rolloutId)+ 关抽屉(若开)+ 刷新列表。
  const handleRollback = async (row: Pick<RolloutRow, 'id'>) => {
    setActingId(row.id);
    try {
      const out = await resources.rollbackRollout(row.id);
      messageApi.success(`已发起回滚(rolloutId=${out.rolloutId})`);
      if (detailRow?.id === row.id) closeDetail();
      reload();
    } catch {
      // 失败由 client 拦截器统一提示。
    } finally {
      setActingId(null);
    }
  };

  // 失败处置按钮组(行内 + 详情 Drawer 共用);按 status/previousTarget 条件渲染,避免点了必 409。
  const failureActions = (row: RolloutRow) => (
    <>
      {canRetry(row) ? (
        <Popconfirm
          title="确认重试该投放?"
          description="将按相同服务重新发起一轮滚动。"
          okText="重试"
          cancelText="取消"
          onConfirm={() => handleRetry(row)}
        >
          <Button type="link" size="small" loading={actingId === row.id}>
            重试
          </Button>
        </Popconfirm>
      ) : null}
      {canRollback(row) ? (
        <Popconfirm
          title="确认回滚该投放?"
          description="将把现状推回上一版(previousTarget)。"
          okText="回滚"
          cancelText="取消"
          onConfirm={() => handleRollback(row)}
        >
          <Button type="link" size="small" danger loading={actingId === row.id}>
            回滚
          </Button>
        </Popconfirm>
      ) : null}
    </>
  );

  // 列(对照 brief):服务 / 命名空间 / 触发 / 模式 / 状态 / 冻结 / 目标 / 创建 / 完成 + 操作。
  // 仅 status / serviceName 为查询列;其余展示列 search:false。
  const columns: ProColumns<RolloutRow>[] = [
    // ── 查询列(仅出现在查询表单,hideInTable)──
    // dataIndex 用 `filterXxx` 前缀,避免与下方同名展示列(serviceName/status)撞 ProTable 表单字段;
    // 经 search.transform 把筛选值映射回后端过滤键(serviceName / status)。
    {
      title: '服务',
      dataIndex: 'filterServiceName',
      key: 'filterServiceName',
      hideInTable: true,
      valueType: 'text',
      fieldProps: { placeholder: '按服务名筛选' },
      search: { transform: (value) => ({ serviceName: value }) },
    },
    {
      title: '状态',
      dataIndex: 'filterStatus',
      key: 'filterStatus',
      hideInTable: true,
      valueType: 'select',
      valueEnum: STATUS_VALUE_ENUM,
      fieldProps: { placeholder: '全部状态', allowClear: true },
      search: { transform: (value) => ({ status: value }) },
    },

    // ── 展示列(均 search:false)──
    {
      title: '服务',
      dataIndex: 'serviceName',
      key: 'serviceName',
      search: false,
      render: (_dom, r) => <Tag color="blue">{r.serviceName}</Tag>,
    },
    { title: '命名空间', dataIndex: 'namespace', key: 'namespace', search: false, render: (_dom, r) => dash(r.namespace) },
    { title: '触发', dataIndex: 'trigger', key: 'trigger', search: false, render: (_dom, r) => renderTrigger(r.trigger) },
    { title: '模式', dataIndex: 'mode', key: 'mode', search: false },
    {
      title: '状态',
      dataIndex: 'rolloutStatus',
      key: 'rolloutStatus',
      search: false,
      render: (_dom, r) => renderStatusTag(r.status),
    },
    {
      title: '冻结',
      dataIndex: 'frozen',
      key: 'frozen',
      search: false,
      // failed + frozen → 「冻结待人工」红 Tag;否则「-」(未冻结无需提示)。
      render: (_dom, r) =>
        r.frozen ? <Tag color="red">冻结待人工</Tag> : '-',
    },
    {
      title: '目标',
      dataIndex: 'target',
      key: 'target',
      search: false,
      render: (_dom, r) =>
        r.target ? (
          <Text style={{ maxWidth: 220 }} ellipsis={{ tooltip: r.target }}>
            {r.target}
          </Text>
        ) : (
          '-'
        ),
    },
    { title: '创建时间', dataIndex: 'createdAt', key: 'createdAt', valueType: 'dateTime', search: false },
    { title: '完成时间', dataIndex: 'finishedAt', key: 'finishedAt', valueType: 'dateTime', search: false },
    {
      title: '操作',
      valueType: 'option',
      key: 'option',
      fixed: 'right',
      render: (_dom, record) => (
        <Space size="small">
          <Button type="link" size="small" onClick={() => openDetail(record)}>
            详情
          </Button>
          {failureActions(record)}
        </Space>
      ),
    },
  ];

  return (
    <>
      {contextHolder}
      <ProTable<RolloutRow>
        actionRef={actionRef}
        rowKey={(r) => r.id}
        columns={columns}
        // P3-10:全局 ns 经 params 透传,变更即触发 ProTable 重拉(空串=全部,request 内剔除)。
        params={{ globalNamespace: namespace?.code ?? '' }}
        // 服务端分页:current/pageSize → 后端 page/pageSize;查询表单产生的 serviceName/status 过滤平铺透传;
        // 空字符串过滤值剔除(不发空参,后端仅按 truthy 过滤);读信封后返回 ProTable 约定结构。
        request={async (params) => {
          const { current, pageSize, globalNamespace, ...filter } = params as typeof params & {
            globalNamespace?: string;
          };
          const cleaned: Record<string, unknown> = {};
          for (const [k, v] of Object.entries(filter)) {
            if (v !== undefined && v !== null && v !== '') cleaned[k] = v;
          }
          // 全局命名空间优先:选了具体 ns 则以其 code 强制过滤(本页无 ns 查询列,直接注入)。
          if (globalNamespace) cleaned.namespace = globalNamespace;
          try {
            const env = await resources.listRollouts<RolloutRow>({
              page: current,
              pageSize,
              ...cleaned,
            });
            return { data: env.rows, total: env.count, success: true };
          } catch {
            // 失败走空态(全局兜底 toast 已由 client 拦截器弹);不让 reject 冒泡成 unhandled rejection。
            return { data: [], total: 0, success: false };
          }
        }}
        pagination={{ showSizeChanger: true }}
        search={{ labelWidth: 'auto', defaultCollapsed: false }}
        options={{ reload: true, density: false, setting: false }}
        toolBarRender={false}
        dateFormatter="string"
        scroll={{ x: 'max-content' }}
      />

      {/* 详情 Drawer:投放行字段 + 底层滚动逐实例进度(投放 → rollingTask 逐实例串起来看)。 */}
      <Drawer
        title={detailRow ? `投放详情 — ${detailRow.serviceName}` : '投放详情'}
        width={820}
        open={detailRow !== null}
        onClose={closeDetail}
        destroyOnClose
        extra={detailRow ? <Space>{failureActions(detailRow)}</Space> : null}
      >
        {detailRow && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {/* frozen:失败即停的半迁移态,顶部显著提示可重试/回滚。 */}
            {detailRow.frozen ? (
              <Alert
                type="error"
                showIcon
                message="失败已冻结(半迁移态),可重试或回滚"
                description="本次投放在滚动中失败并冻结,部分实例可能已切到新目标。请重试推进到一致,或回滚到上一版。"
              />
            ) : null}

            {detailLoading ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: 32 }}>
                <Spin />
              </div>
            ) : detailErrored ? (
              <Alert
                type="error"
                showIcon
                message="加载投放详情失败"
                description="请稍后重试。"
                action={
                  <Button size="small" onClick={() => void fetchDetail(detailRow.id)}>
                    重试
                  </Button>
                }
              />
            ) : detail ? (
              <>
                {/* 投放行字段(只读概览)。 */}
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label="服务">{detail.serviceName}</Descriptions.Item>
                  <Descriptions.Item label="命名空间">{dash(detail.namespace)}</Descriptions.Item>
                  <Descriptions.Item label="状态">{renderStatusTag(detail.status)}</Descriptions.Item>
                  <Descriptions.Item label="触发">{renderTrigger(detail.trigger)}</Descriptions.Item>
                  <Descriptions.Item label="模式">{detail.mode}</Descriptions.Item>
                  <Descriptions.Item label="强制(force)">{detail.force ? '是' : '否'}</Descriptions.Item>
                  <Descriptions.Item label="目标" span={2}>{dash(detail.target)}</Descriptions.Item>
                  <Descriptions.Item label="上一版" span={2}>{dash(detail.previousTarget)}</Descriptions.Item>
                  {detail.error ? (
                    <Descriptions.Item label="错误" span={2}>
                      <Text type="danger">{detail.error}</Text>
                    </Descriptions.Item>
                  ) : null}
                  <Descriptions.Item label="创建时间">{dash(detail.createdAt)}</Descriptions.Item>
                  <Descriptions.Item label="完成时间">{dash(detail.finishedAt)}</Descriptions.Item>
                </Descriptions>

                {/* 底层滚动逐实例进度(可复用组件;无关联滚动 → 组件内占位「暂无滚动进度」)。 */}
                <div>
                  <Text strong>逐实例进度</Text>
                  <div style={{ marginTop: 8 }}>
                    <RollingTaskNodesTable task={detail.rollingTask} />
                  </div>
                </div>
              </>
            ) : null}
          </Space>
        )}
      </Drawer>
    </>
  );
}
