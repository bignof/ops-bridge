import { useRef, useState } from 'react';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import { Alert, Button, Dropdown, Input, Modal, Space, Tag, Typography, message } from 'antd';
import { DownOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';
import type { NodeAction, NodeActionBody, NodeActionOut, NodeRow } from '../api/resources';

const { Text } = Typography;

// 行操作菜单项 → 具体动作意图(动作 + 模式 + 可选 allowLastInstance)。
// 二次确认 Modal 据此展示「动作 + 模式 + 目标」并下发 dispatchNodeAction。
interface ActionIntent {
  /** 菜单/确认框展示的中文动作名(含模式),如「停止(force)」。 */
  label: string;
  action: NodeAction;
  /** start 无 mode;stop/redeploy 必传;restart 缺省 graceful。 */
  mode?: 'graceful' | 'force';
  /** force stop 显式 allowLastInstance=false(不允许优雅 drain 最后一个实例时强制)。 */
  allowLastInstance?: boolean;
}

// 行操作清单(对照 brief):启动 / 停止(优雅|force) / 重启(优雅|force) / 重部署(优雅|force)。
//  - 启动:action='start',无 mode。
//  - 停止:action='stop' + mode(必传);force 带 allowLastInstance=false。
//  - 重启:action='restart' + mode。
//  - 重部署:action='redeploy' + mode(必传)。
const ROW_ACTIONS: ActionIntent[] = [
  { label: '启动', action: 'start' },
  { label: '停止(优雅)', action: 'stop', mode: 'graceful' },
  { label: '停止(force)', action: 'stop', mode: 'force', allowLastInstance: false },
  { label: '重启(优雅)', action: 'restart', mode: 'graceful' },
  { label: '重启(force)', action: 'restart', mode: 'force' },
  { label: '重部署(优雅)', action: 'redeploy', mode: 'graceful' },
  { label: '重部署(force)', action: 'redeploy', mode: 'force' },
];

// 在线状态 Tag(online=true → 绿色「在线」,否则灰「离线」)。
const renderOnlineTag = (online: boolean) =>
  online ? <Tag color="green">在线</Tag> : <Tag>离线</Tag>;

// 健康数:degraded(健康计数不可信)或 null(后端未知)→ 显「-」;否则显数字。
const renderHealthy = (record: NodeRow) =>
  record.degraded || record.healthyCount === null || record.healthyCount === undefined
    ? '-'
    : String(record.healthyCount);

/**
 * 节点页(resource `nodes`):平台 Service 表驱动的 (agent×service) **只读列表 + 行级运维操作**。
 *
 * 列表:服务端分页(ProTable `request` 映射 current/pageSize → 后端 page/pageSize,读统一信封)。
 * 列:serviceCode / namespaceCode(=agentId) / dir / online(Tag) / healthyCount(degraded→「-」) / defaultImage。
 *
 * 行操作:「操作」Dropdown 七项(启停重启重部署,各 优雅/force)→ 打开**二次确认 Modal**:
 *  - 展示 动作 + 模式 + 目标(agentId/serviceCode),内含 Input;**确认按钮仅在输入 === 该行 serviceCode 时可点**
 *    (破坏性操作护栏,仿 GitHub 输入名确认)。
 *  - 确认 → dispatchNodeAction(agentId, serviceCode, action, body):start 不传 mode;force stop 带 allowLastInstance=false。
 *
 * 成功:message.success(含 kind 与 requestId/taskId)+ 刷新表格(healthyCount 反映变化)。
 * 失败:dispatchNodeAction 资源层 opt-out 全局兜底,故此处按 e.response?.status 本地精确提示
 *       (404/400/409/502 各文案),**非预期状态也兜底提示、不静默吞**(A2);401 仍由拦截器统一处理。
 *
 * 注(结果轮询):本期成功后刷新列表 + toast requestId/taskId 即可;逐命令状态轮询留后续(审计页 T12 看状态)。
 */
export default function NodesPage() {
  const actionRef = useRef<ActionType>();
  const [messageApi, contextHolder] = message.useMessage();

  // 二次确认 Modal 状态:当前待确认的 (行 × 动作意图);为空表示关闭。
  const [pending, setPending] = useState<{ row: NodeRow; intent: ActionIntent } | null>(null);
  // 护栏输入框当前值;须 === 行 serviceCode 才放行确认。
  const [confirmText, setConfirmText] = useState('');
  // 下发中:防确认按钮重复点击。
  const [submitting, setSubmitting] = useState(false);

  const reload = () => actionRef.current?.reload();

  // 打开二次确认 Modal(重置护栏输入)。
  const openConfirm = (row: NodeRow, intent: ActionIntent) => {
    setConfirmText('');
    setPending({ row, intent });
  };

  const closeConfirm = () => {
    setPending(null);
    setConfirmText('');
  };

  // 成功提示文案:区分同步命令(requestId)与滚动任务(taskId)。
  const successText = (out: NodeActionOut): string => {
    if (out.kind === 'rolling') return `滚动重启已触发(taskId=${out.taskId ?? '-'})`;
    return `已下发(requestId=${out.requestId ?? '-'})`;
  };

  // 失败提示:dispatchNodeAction opt-out 全局兜底,故此处自管全部错误 UX(不静默吞,A2)。
  //  - 404:非台账(该 agent×service 不在平台 Service 表)。
  //  - 400:缺 nacos/image 或缺 mode(后端参数校验)。
  //  - 409:无健康实例可优雅 drain。
  //  - 502:hub 失败(下游不可用)。
  //  - 其余非 401:通用兜底(不静默吞);401 由 client 拦截器统一处理(清 token + 跳登录)。
  const handleError = (e: unknown) => {
    const status =
      typeof e === 'object' && e && 'response' in e
        ? (e as { response?: { status?: number } }).response?.status
        : undefined;
    if (status === 404) messageApi.error('该节点不在平台台账中(未登记的 agent×service)');
    else if (status === 400) messageApi.error('参数无效:缺少 Nacos 服务名 / 镜像,或缺少操作模式');
    else if (status === 409) messageApi.error('无健康实例可优雅 drain,请改用 force 或稍后重试');
    else if (status === 502) messageApi.error('下发失败:控制面(hub)不可用,请稍后重试');
    else if (status !== 401) messageApi.error('操作失败,请稍后重试');
  };

  // 确认下发:构造 body(start 无 mode;其余带 mode;force stop 带 allowLastInstance=false)→ dispatch。
  const handleConfirm = async () => {
    if (!pending) return;
    const { row, intent } = pending;
    const body: NodeActionBody = {};
    if (intent.mode) body.mode = intent.mode;
    if (intent.allowLastInstance !== undefined) body.allowLastInstance = intent.allowLastInstance;

    setSubmitting(true);
    try {
      const out = await resources.dispatchNodeAction(row.agentId, row.serviceCode, intent.action, body);
      messageApi.success(successText(out));
      closeConfirm();
      reload(); // 刷新列表(healthyCount 反映变化)
    } catch (e) {
      handleError(e);
      // 失败不关闭 Modal,便于用户重试 / 改选(护栏输入仍在)。
    } finally {
      setSubmitting(false);
    }
  };

  // 列(对照 brief):serviceCode / namespaceCode(=agentId) / dir / online(Tag) / healthyCount / defaultImage + 操作。
  const columns: ProColumns<NodeRow>[] = [
    { title: '服务编码', dataIndex: 'serviceCode', key: 'serviceCode', copyable: true },
    // namespaceCode = agentId(命名空间可读名);列直接用后端回的 namespaceCode。
    { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode' },
    { title: '目录', dataIndex: 'dir', key: 'dir' },
    {
      title: '在线',
      dataIndex: 'online',
      key: 'online',
      render: (_dom, r) => renderOnlineTag(r.online),
    },
    {
      title: '健康实例数',
      dataIndex: 'healthyCount',
      key: 'healthyCount',
      // degraded 行 / null → 「-」(健康计数不可信或未知)。
      render: (_dom, r) => renderHealthy(r),
    },
    { title: '默认镜像', dataIndex: 'defaultImage', key: 'defaultImage', ellipsis: true },
    {
      title: '操作',
      valueType: 'option',
      key: 'option',
      render: (_dom, record) => (
        <Dropdown
          trigger={['click']}
          menu={{
            items: ROW_ACTIONS.map((intent) => ({ key: intent.label, label: intent.label })),
            onClick: ({ key }) => {
              const intent = ROW_ACTIONS.find((a) => a.label === key);
              if (intent) openConfirm(record, intent);
            },
          }}
        >
          {/* 用 Button 作触发器:语义为 button(无障碍/可测),点击展开七项运维动作菜单。 */}
          <Button size="small">
            <Space size={4}>
              操作
              <DownOutlined />
            </Space>
          </Button>
        </Dropdown>
      ),
    },
  ];

  // 确认按钮放行条件:护栏输入 === 该行 serviceCode(破坏性操作护栏)。
  const confirmReady = pending !== null && confirmText === pending.row.serviceCode;

  return (
    <>
      {contextHolder}
      <ProTable<NodeRow>
        actionRef={actionRef}
        rowKey={(r) => `${r.agentId}:${r.serviceCode}`}
        columns={columns}
        // 服务端分页:current/pageSize → 后端 page/pageSize;读统一信封后返回 ProTable 约定结构。
        request={async (params) => {
          const { current, pageSize } = params;
          const env = await resources.listNodes<NodeRow>({ page: current, pageSize });
          return { data: env.rows, total: env.count, success: true };
        }}
        pagination={{ showSizeChanger: true }}
        search={false}
        options={{ reload: true, density: false, setting: false }}
        toolBarRender={false}
        dateFormatter="string"
      />

      {/* 二次确认 Modal:展示 动作 + 模式 + 目标,输入 serviceCode 核对后放行(破坏性操作护栏)。 */}
      <Modal
        title="确认运维操作"
        open={pending !== null}
        onCancel={closeConfirm}
        maskClosable={false}
        destroyOnClose
        footer={[
          <Button key="cancel" onClick={closeConfirm}>
            取消
          </Button>,
          <Button
            key="confirm"
            type="primary"
            danger
            // 护栏:输入 !== 行 serviceCode 时禁用,杜绝误操作。
            disabled={!confirmReady}
            loading={submitting}
            onClick={handleConfirm}
          >
            确认
          </Button>,
        ]}
      >
        {pending && (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Alert
              type="warning"
              showIcon
              message="破坏性运维操作,请确认目标后输入服务编码核对再执行。"
            />
            <div>
              动作:<Text strong>{pending.intent.label}</Text>
            </div>
            {pending.intent.mode && (
              <div>
                模式:<Text strong>{pending.intent.mode}</Text>
              </div>
            )}
            <div>
              目标:<Text code>{pending.row.agentId}</Text> / <Text code>{pending.row.serviceCode}</Text>
            </div>
            <div>
              请输入服务编码 <Text strong>{pending.row.serviceCode}</Text> 以确认:
            </div>
            <Input
              autoFocus
              placeholder={`请输入 serviceCode:${pending.row.serviceCode}`}
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              onPressEnter={() => {
                if (confirmReady && !submitting) handleConfirm();
              }}
            />
          </Space>
        )}
      </Modal>
    </>
  );
}
