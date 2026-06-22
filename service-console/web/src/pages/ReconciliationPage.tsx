import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Form,
  Input,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import * as resources from '../api/resources';
import type {
  ManagedDownServiceRow,
  ReconciliationResult,
  UnmanagedServiceRow,
} from '../api/resources';

const { Text, Paragraph } = Typography;

// 命名空间下拉行(纳管表单关联选择 options 用,value=id、label=code)。
interface NamespaceOption {
  id: string | number;
  code: string;
  name?: string;
}

// 命名空间选项 pageSize:取后端硬上限 200(各 list 端点 Query le=200,前后端一致;>200 需远程搜索,属后续增强)。
const NS_OPTIONS_PAGE_SIZE = 200;

// 可空文本统一渲染:null/undefined/空串 → 「-」,否则原值。
const dash = (v?: string | null) => (v ? v : '-');

// 纳管表单提交值(预填后可改):命名空间 id、服务编码、Nacos 服务名。
interface AdoptFormValues {
  namespaceId: string | number;
  serviceCode: string;
  nacosServiceName: string;
}

/**
 * 服务对账 / 纳管页(resource `nodes/reconciliation` 只读 + 纳管走既有 `services` create)。
 *
 * 把「意图」(平台 Service 台账)与「现实」(agent 发现实例)按 `nacosServiceName` 关联,实时算三态:
 *  - **已发现未纳管(收件箱)**:在跑但无对应 Service 的 nacosService。每行「纳管」→ 预填抽屉
 *    (serviceCode/nacosServiceName 默认 = nacosService;命名空间从该行首个 agentId 解析 code→id 预选,
 *    可改)→ 调 `create('services', ...)` 建 Service → 成功后刷新对账(该项即从收件箱消失)。
 *  - **纳管了但没实例(该起没起)**:Service.nacosServiceName 非空却无 active 发现实例匹配,只读提示。
 *  - **版本漂移**:本期恒空(实例暂未上报插件版本),显占位「暂无」。
 *
 * 纳管动作**无专用端点**:= 预填 + 调既有 `POST /api/services`(对齐设计;namespace 由用户选 agent 决定)。
 * 加载/空态/错误态对齐既有页面;纳管成功/失败给 toast。对账数据不分页(后端回全集),前端整表展示。
 */
export default function ReconciliationPage() {
  const [messageApi, contextHolder] = message.useMessage();
  const [form] = Form.useForm<AdoptFormValues>();

  // 对账数据 + 加载/错误态(整页拉取,不分页)。
  const [data, setData] = useState<ReconciliationResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [errored, setErrored] = useState(false);

  // 命名空间选项(纳管表单关联选择用);code→id 映射供纳管时预选命名空间。
  const [nsOptions, setNsOptions] = useState<NamespaceOption[]>([]);

  // 纳管抽屉:当前待纳管的收件箱行(null = 关闭);提交中防重复点击。
  const [adopting, setAdopting] = useState<UnmanagedServiceRow | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // 拉对账三态(失败置错误态,由 client 拦截器统一兜底 toast;本页只读拉取无需本地精确提示)。
  const fetchReconciliation = useCallback(async () => {
    setLoading(true);
    setErrored(false);
    try {
      const res = await resources.getReconciliation();
      setData(res);
    } catch {
      setErrored(true);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // 拉命名空间选项(纳管表单关联选择 + code→id 预选用);失败静默(表单仍可手选,空选项时报错由提交兜底)。
  const fetchNamespaces = useCallback(async () => {
    try {
      const env = await resources.list<NamespaceOption>('namespaces', {
        pageSize: NS_OPTIONS_PAGE_SIZE,
      });
      setNsOptions(env.rows);
    } catch {
      setNsOptions([]);
    }
  }, []);

  useEffect(() => {
    void fetchReconciliation();
    void fetchNamespaces();
  }, [fetchReconciliation, fetchNamespaces]);

  // code→id 映射(发现侧 agentId 即命名空间 code;纳管时据此把 agentId 预选成 namespaceId)。
  const codeToNsId = useMemo(() => {
    const m = new Map<string, string | number>();
    for (const ns of nsOptions) m.set(ns.code, ns.id);
    return m;
  }, [nsOptions]);

  // 命名空间下拉 options(value=id、label=code,name 兜底;showSearch 本地过滤)。
  const namespaceSelectOptions = useMemo(
    () => nsOptions.map((n) => ({ label: n.code || String(n.id), value: n.id })),
    [nsOptions],
  );

  // 打开纳管抽屉:预填 serviceCode/nacosServiceName = nacosService;命名空间从首个 agentId 解析 code→id 预选
  // (解析不到 = 该 agentId 未登记命名空间台账,留空让用户手选,并在抽屉内提示)。
  const openAdopt = (row: UnmanagedServiceRow) => {
    const firstAgent = row.agentIds[0];
    const presetNsId = firstAgent ? codeToNsId.get(firstAgent) : undefined;
    setAdopting(row);
    form.setFieldsValue({
      namespaceId: presetNsId,
      serviceCode: row.nacosService,
      nacosServiceName: row.nacosService,
    } as AdoptFormValues);
  };

  const closeAdopt = () => {
    setAdopting(null);
    form.resetFields();
  };

  // 纳管提交:校验后调既有 create('services', ...) 建 Service;成功关抽屉 + 刷新对账(该项从收件箱消失)。
  // 失败按状态码精确提示:409 = 命名空间内 service_code 重复;其余非 401 通用兜底(create 资源层 opt-out
  // 全局拦截,故此处必须自管错误 UX,不静默吞);401 由 client 拦截器统一处理。
  const handleAdopt = async () => {
    let values: AdoptFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return; // 校验未过(必填未填),表单已就地标红,不提交
    }
    setSubmitting(true);
    try {
      await resources.create('services', {
        namespaceId: values.namespaceId,
        serviceCode: values.serviceCode,
        nacosServiceName: values.nacosServiceName,
      });
      messageApi.success(`已纳管 ${values.serviceCode},去「服务插件」绑定插件`);
      closeAdopt();
      await fetchReconciliation(); // 刷新对账:该 nacosService 已纳管,应从收件箱消失
    } catch (e) {
      const status =
        typeof e === 'object' && e && 'response' in e
          ? (e as { response?: { status?: number } }).response?.status
          : undefined;
      if (status === 409) messageApi.error('该命名空间下服务编码已存在');
      else if (status !== 401) messageApi.error('纳管失败,请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  // ── 收件箱(runningButUnmanaged)列:命名空间(agentIds)/ 服务名(nacosService)/ 发现实例 / 操作 ──
  const inboxColumns: ColumnsType<UnmanagedServiceRow> = [
    {
      title: '命名空间',
      dataIndex: 'agentIds',
      key: 'agentIds',
      // 同一 nacosService 可跨多 agent;逐个 agentId 显为 tag(承载该服务的命名空间)。
      render: (_v, r) =>
        r.agentIds.length ? (
          <Space size={[4, 4]} wrap>
            {r.agentIds.map((a) => (
              <Tag key={a}>{a}</Tag>
            ))}
          </Space>
        ) : (
          '-'
        ),
    },
    {
      title: '服务名',
      dataIndex: 'nacosService',
      key: 'nacosService',
      render: (_v, r) => <Tag color="blue">{r.nacosService}</Tag>,
    },
    {
      title: '发现实例',
      dataIndex: 'instanceCount',
      key: 'instanceCount',
      render: (_v, r) => r.instanceCount,
    },
    {
      title: '操作',
      key: 'option',
      render: (_v, r) => (
        <Button type="link" size="small" onClick={() => openAdopt(r)}>
          纳管
        </Button>
      ),
    },
  ];

  // ── 该起没起(managedButDown)列:服务编码 / Nacos 服务名 / 命名空间(只读) ──
  const downColumns: ColumnsType<ManagedDownServiceRow> = [
    {
      title: '服务编码',
      dataIndex: 'serviceCode',
      key: 'serviceCode',
    },
    {
      title: 'Nacos 服务名',
      dataIndex: 'nacosServiceName',
      key: 'nacosServiceName',
      render: (_v, r) => <Tag color="blue">{r.nacosServiceName}</Tag>,
    },
    {
      title: '命名空间',
      dataIndex: 'namespaceCode',
      key: 'namespaceCode',
      render: (_v, r) => dash(r.namespaceCode),
    },
  ];

  // 加载中:整页居中 Spin(对齐 lazyPage fallback 风格)。
  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}>
        <Spin />
      </div>
    );
  }

  // 错误态:加载失败给可重试入口(全局兜底 toast 已弹;此处给页面级重试按钮,不静默卡空白)。
  if (errored || !data) {
    return (
      <>
        {contextHolder}
        <Alert
          type="error"
          showIcon
          message="加载对账数据失败"
          description="请检查网络或稍后重试。"
          action={
            <Button size="small" onClick={() => void fetchReconciliation()}>
              重试
            </Button>
          }
        />
      </>
    );
  }

  return (
    <>
      {contextHolder}

      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {/* 顶部说明 + 刷新:对账 = 意图(已纳管 Service)⋈ 现实(自动发现实例),by nacos 服务名,实时算。 */}
        <Card>
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            <Space style={{ width: '100%', justifyContent: 'space-between' }}>
              <Text strong style={{ fontSize: 16 }}>
                服务对账
              </Text>
              <Button onClick={() => void fetchReconciliation()}>刷新</Button>
            </Space>
            <Text type="secondary">
              对账 = 意图(已纳管 Service)⋈ 现实(自动发现实例),按 nacos 服务名实时计算。
            </Text>
          </Space>
        </Card>

        {/* ① 已发现未纳管(收件箱):在跑但无对应 Service,每行可「纳管」(预填建 Service)。 */}
        <Card
          title={
            <Space>
              <span>已发现未纳管</span>
              <Tag color="orange">{data.runningButUnmanaged.length}</Tag>
            </Space>
          }
        >
          <Paragraph type="secondary" style={{ marginTop: 0 }}>
            在跑但其 nacos 服务名不属任何已纳管服务。点「纳管」预填命名空间 / 服务编码 / nacos 名后建
            Service;纳管后该项从此列表消失,可去「服务插件」为其绑定插件。
          </Paragraph>
          {data.runningButUnmanaged.length ? (
            <Table<UnmanagedServiceRow>
              rowKey={(r) => r.nacosService}
              columns={inboxColumns}
              dataSource={data.runningButUnmanaged}
              pagination={false}
              size="middle"
            />
          ) : (
            <Empty description="无未纳管服务 —— 都已纳管" />
          )}
        </Card>

        {/* ② 纳管了但没实例(该起没起):只读提示。 */}
        <Card
          title={
            <Space>
              <span>纳管了但没实例</span>
              <Tag color="red">{data.managedButDown.length}</Tag>
            </Space>
          }
        >
          <Paragraph type="secondary" style={{ marginTop: 0 }}>
            已纳管(Service 配了 nacos 服务名)但当前无任何活跃发现实例 —— 该起没起,请检查部署或启动该服务。
          </Paragraph>
          {data.managedButDown.length ? (
            <Table<ManagedDownServiceRow>
              rowKey={(r) => r.serviceCode}
              columns={downColumns}
              dataSource={data.managedButDown}
              pagination={false}
              size="middle"
            />
          ) : (
            <Empty description="无异常 —— 已纳管服务均有活跃实例" />
          )}
        </Card>

        {/* ③ 版本漂移:本期恒空(实例暂未上报插件版本),显占位。 */}
        <Card title="版本漂移">
          <Empty description="暂无(待实例携带插件版本后启用)" />
        </Card>
      </Space>

      {/* 纳管抽屉:预填 serviceCode/nacosServiceName = nacosService;命名空间从首个 agentId 解析预选(可改)。 */}
      <Drawer
        title="从发现纳管服务"
        width={480}
        open={adopting !== null}
        onClose={closeAdopt}
        destroyOnClose
        footer={
          <Space style={{ float: 'right' }}>
            <Button onClick={closeAdopt}>取消</Button>
            <Button type="primary" loading={submitting} onClick={() => void handleAdopt()}>
              纳管
            </Button>
          </Space>
        }
      >
        {adopting && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Alert
              type="info"
              showIcon
              message="已从发现预填命名空间、服务编码与 nacos 名(默认同值,可分别改)。确定后建 Service 并从未纳管移除。"
            />
            <Form<AdoptFormValues> form={form} layout="vertical">
              <Form.Item
                label="命名空间"
                name="namespaceId"
                rules={[{ required: true, message: '请选择要纳管到的命名空间' }]}
                extra={
                  adopting.agentIds.length > 1
                    ? `该服务跨多个命名空间(${adopting.agentIds.join('、')}),请确认纳管到哪个`
                    : undefined
                }
              >
                <Select
                  showSearch
                  optionFilterProp="label"
                  placeholder="请选择命名空间"
                  options={namespaceSelectOptions}
                  notFoundContent={
                    nsOptions.length ? undefined : (
                      <Tooltip title="未拉到命名空间台账,请先在「命名空间」页创建">
                        <span>无可选命名空间</span>
                      </Tooltip>
                    )
                  }
                />
              </Form.Item>
              <Form.Item
                label="服务编码"
                name="serviceCode"
                rules={[{ required: true, message: '请输入服务编码' }]}
              >
                <Input placeholder="服务编码(分发标识)" />
              </Form.Item>
              <Form.Item
                label="Nacos 服务名"
                name="nacosServiceName"
                rules={[{ required: true, message: '请输入 Nacos 服务名' }]}
                extra="对账 / 滚动部署的关联键,默认同服务编码,可分别编辑"
              >
                <Input placeholder="Nacos 服务名" />
              </Form.Item>
            </Form>
          </Space>
        )}
      </Drawer>
    </>
  );
}
