import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Form,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { ApiOutlined, PlusOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';

const { Text } = Typography;

// ── 类型(均对齐 P1a 契约,全 camelCase)─────────────────────────────────────────
// 服务下拉项(顶部「选服务」用):list('services')。
interface ServiceOption {
  id: string | number;
  serviceCode: string;
  name?: string;
}
// 插件下拉项(「绑定插件」弹窗用):list('plugins')。
interface PluginOption {
  id: string | number;
  code: string;
}
// 服务-插件绑定行(service-plugins?serviceId=);**后端不回版本**(见 service_plugins.py),
// 当前版本另从 releases 主表 join 取(byKey)。
interface ServicePluginRow {
  id: string | number;
  serviceId: string | number;
  pluginId: string | number;
  pluginCode?: string;
}
// release 行(listReleaseHistory / listReleases 回 ReleaseOut 子集):版本历史 + 改版本(重新激活)用。
interface ReleaseRow {
  id: string | number;
  serviceId: string | number;
  pluginId: string | number;
  version?: string;
  versionOrder?: number;
  isActive?: boolean;
  isRolledBack?: boolean;
  publishTime?: string;
}

// 各 list 端点硬卡 pageSize le=200(后端 Query le=200,前后端必须一致;>200 会 422)。
const OPTIONS_PAGE_SIZE = 200;

// 「当前版本」join 键:(serviceId, pluginId) → 该绑定 active 版本号。serviceId/pluginId 可能为
// number 或 string(后端回 number,这里统一 String 拼键,避免类型不一致漏匹配)。
const versionKey = (serviceId: string | number, pluginId: string | number) =>
  `${String(serviceId)}::${String(pluginId)}`;

/**
 * 服务配置(二级页,resource `service-plugins` + `releases`):为某个服务配置「装哪些插件」与「各插件版本」。
 *
 * 形态对齐原型「服务 → 配置」二级页(docs/plugin-platform-prototype.zh-CN.html #serviceplugins):
 *  - 顶部选服务(下拉,showSearch)。选定后才展示该服务的绑定配置(原型从服务列表进入并锁定服务;
 *    本期按任务允许的「下拉选服务」实现,自包含、不动共享 CrudTable / 服务页,详见任务说明)。
 *  - 「绑定插件」:从未绑定的插件里选一个 → create('service-plugins', {serviceId, pluginId})。
 *  - 每条绑定:展示「当前版本」+「版本历史 / 改版本 / 解绑」。
 *    · 当前版本:service-plugins **不回版本**,从 releases 主表 join((serviceId,pluginId)→version);
 *      绑定了但还没发布过版本则显「未发布」。
 *    · 解绑:remove('service-plugins', id)(关联行是多对多事实,改即删)。
 *    · 改版本 / 版本历史:同一抽屉,listReleaseHistory({serviceId,pluginId}) 列该绑定全部已发布版本,
 *      非 active 行可「重新激活」reactivate({spvId})=切到该版本(原型「选用并发布」);active 行显「当前」。
 *      **发布全新版本**不在本页(需先上传版本再 publish),走侧栏「插件发布」页 → 此处标 TODO。
 *
 * ⚠️ 当前版本的「全集」限制:releases 无 `?serviceId=` 单独过滤(仅 serviceId+pluginId 同传取历史),
 *    故主表一次性拉 pageSize=200 在前端 join。全局 active 绑定 >200 时会截断(超出的显「未发布」)。
 *    这是已知边界,>200 需后端支持按 serviceId 过滤主表(TODO,不臆造端点)。
 */
export default function ServicePluginsPage() {
  const [messageApi, contextHolder] = message.useMessage();

  // ── 顶部选服务 ───────────────────────────────────────────────────────────────
  const [serviceOptions, setServiceOptions] = useState<ServiceOption[]>([]);
  const [serviceId, setServiceId] = useState<string | number | undefined>(undefined);
  // 服务下拉自身的加载/错误态(初次拉服务列表);失败给页面级重试。
  const [svcLoading, setSvcLoading] = useState(true);
  const [svcErrored, setSvcErrored] = useState(false);

  // ── 选定服务后的绑定配置 ─────────────────────────────────────────────────────
  const [bindings, setBindings] = useState<ServicePluginRow[]>([]);
  // (serviceId,pluginId)→ 当前 active 版本号(来自 releases 主表)。
  const [versionByKey, setVersionByKey] = useState<Map<string, string>>(new Map());
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [bindingsErrored, setBindingsErrored] = useState(false);

  // 「绑定插件」弹窗:候选插件、选中项、提交中。
  const [bindOpen, setBindOpen] = useState(false);
  const [pluginOptions, setPluginOptions] = useState<PluginOption[]>([]);
  const [pluginToBind, setPluginToBind] = useState<string | number | undefined>(undefined);
  const [binding, setBinding] = useState(false);

  // 「改版本 / 版本历史」抽屉:当前查看的绑定(为空=关闭)。
  const [versionDrawer, setVersionDrawer] = useState<ServicePluginRow | null>(null);
  const [history, setHistory] = useState<ReleaseRow[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyErrored, setHistoryErrored] = useState(false);
  // 重新激活提交中的目标 spvId(用于该行按钮 loading;同时全表禁用避免并发切换)。
  const [reactivatingId, setReactivatingId] = useState<string | number | null>(null);

  // 选中服务的展示标签(serviceCode(name))。
  const selectedService = useMemo(
    () => serviceOptions.find((s) => String(s.id) === String(serviceId)),
    [serviceOptions, serviceId],
  );

  // 服务下拉 options(label=serviceCode(name),value=id;showSearch 本地过滤)。
  const serviceSelectOptions = useMemo(
    () =>
      serviceOptions.map((s) => ({
        label: s.name ? `${s.serviceCode}(${s.name})` : s.serviceCode,
        value: s.id,
      })),
    [serviceOptions],
  );

  // 拉服务列表(顶部选服务用);失败置错误态,给页面级重试。
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

  useEffect(() => {
    void fetchServices();
  }, [fetchServices]);

  // 拉选定服务的绑定 + 当前版本 map。绑定走 service-plugins?serviceId=(源真相);版本走 releases
  // 主表(无 serviceId 单过滤,拉 pageSize=200 前端 join,见类头⚠️)。两请求并行;绑定失败置错误态,
  // 版本失败仅降级为「未发布」(不阻断绑定展示,因为版本是附加信息)。
  const fetchBindings = useCallback(async (svcId: string | number) => {
    setBindingsLoading(true);
    setBindingsErrored(false);
    try {
      const [spEnv, relEnv] = await Promise.all([
        resources.list<ServicePluginRow>('service-plugins', {
          serviceId: svcId,
          pageSize: OPTIONS_PAGE_SIZE,
        }),
        // releases 主表(不传 filter → 每 (service,plugin) 一行 active,含 version)。
        resources
          .listReleases<ReleaseRow>({ pageSize: OPTIONS_PAGE_SIZE })
          .catch(() => ({ count: 0, rows: [] as ReleaseRow[], page: 1, pageSize: 0, totalPage: 0 })),
      ]);
      setBindings(spEnv.rows);
      const map = new Map<string, string>();
      for (const r of relEnv.rows) {
        if (r.version) map.set(versionKey(r.serviceId, r.pluginId), r.version);
      }
      setVersionByKey(map);
    } catch {
      setBindingsErrored(true);
      setBindings([]);
      setVersionByKey(new Map());
    } finally {
      setBindingsLoading(false);
    }
  }, []);

  // 选定服务变化时(含切换)重拉绑定;未选则清空。
  useEffect(() => {
    if (serviceId === undefined || serviceId === null || serviceId === '') {
      setBindings([]);
      setVersionByKey(new Map());
      return;
    }
    void fetchBindings(serviceId);
  }, [serviceId, fetchBindings]);

  // 已绑定的 pluginId 集合(「绑定插件」弹窗据此剔除已绑项,防重复绑定 409)。
  const boundPluginIds = useMemo(
    () => new Set(bindings.map((b) => String(b.pluginId))),
    [bindings],
  );

  // ── 绑定插件 ─────────────────────────────────────────────────────────────────
  // 打开弹窗:拉全量插件(剔除已绑),重置选中项。
  const openBind = async () => {
    setPluginToBind(undefined);
    setBindOpen(true);
    try {
      const env = await resources.list<PluginOption>('plugins', { pageSize: OPTIONS_PAGE_SIZE });
      setPluginOptions(env.rows);
    } catch {
      setPluginOptions([]); // 失败留空,弹窗内 notFoundContent 提示;提交按钮因无选项不可用
    }
  };

  // 候选插件下拉 options(剔除已绑定的)。
  const bindablePluginOptions = useMemo(
    () =>
      pluginOptions
        .filter((p) => !boundPluginIds.has(String(p.id)))
        .map((p) => ({ label: p.code || String(p.id), value: p.id })),
    [pluginOptions, boundPluginIds],
  );

  // 提交绑定:create('service-plugins', {serviceId, pluginId}) → 成功关弹窗 + 刷新。
  // create 资源层 opt-out 全局兜底,故此处自管错误 UX:409=重复绑定(理论上已被剔除,仍兜底),其余通用。
  const handleBind = async () => {
    if (serviceId === undefined || pluginToBind === undefined) return;
    setBinding(true);
    try {
      await resources.create('service-plugins', { serviceId, pluginId: pluginToBind });
      messageApi.success('绑定成功');
      setBindOpen(false);
      await fetchBindings(serviceId);
    } catch (e) {
      const status =
        typeof e === 'object' && e && 'response' in e
          ? (e as { response?: { status?: number } }).response?.status
          : undefined;
      if (status === 409) messageApi.error('该插件已绑定该服务,请勿重复关联');
      else if (status !== 401) messageApi.error('绑定失败,请稍后重试');
    } finally {
      setBinding(false);
    }
  };

  // ── 解绑 ─────────────────────────────────────────────────────────────────────
  // remove('service-plugins', id);失败由 client 拦截器统一兜底 toast。
  const handleUnbind = async (row: ServicePluginRow) => {
    try {
      await resources.remove('service-plugins', row.id);
      messageApi.success('解绑成功');
      if (serviceId !== undefined) await fetchBindings(serviceId);
    } catch {
      // 删除失败由 client 拦截器提示;此处不额外处理。
    }
  };

  // ── 改版本 / 版本历史 ────────────────────────────────────────────────────────
  // 打开抽屉:拉该绑定全部已发布版本(listReleaseHistory,serviceId+pluginId 服务端过滤)。
  const fetchHistory = useCallback(async (row: ServicePluginRow) => {
    setHistoryLoading(true);
    setHistoryErrored(false);
    try {
      const env = await resources.listReleaseHistory<ReleaseRow>({
        serviceId: row.serviceId,
        pluginId: row.pluginId,
        pageSize: OPTIONS_PAGE_SIZE,
      });
      setHistory(env.rows);
    } catch {
      setHistoryErrored(true);
      setHistory([]);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const openVersionDrawer = (row: ServicePluginRow) => {
    setVersionDrawer(row);
    void fetchHistory(row);
  };

  const closeVersionDrawer = () => {
    setVersionDrawer(null);
    setHistory([]);
  };

  // 改版本(重新激活历史版本):reactivate({spvId}) → 切该版本为唯一 active。
  // 成功后刷新抽屉历史 + 主表「当前版本」。失败由 client 拦截器统一兜底(取后端 detail)。
  const handleReactivate = async (rel: ReleaseRow) => {
    setReactivatingId(rel.id);
    try {
      await resources.reactivate({ spvId: rel.id });
      messageApi.success('已切换版本');
      if (versionDrawer) await fetchHistory(versionDrawer);
      if (serviceId !== undefined) await fetchBindings(serviceId);
    } catch {
      // 失败由 client 拦截器统一提示。
    } finally {
      setReactivatingId(null);
    }
  };

  // 绑定表列:插件 / 当前版本(join releases)/ 操作(版本历史 改版本 解绑)。
  const bindingColumns: ColumnsType<ServicePluginRow> = [
    {
      title: '插件',
      dataIndex: 'pluginCode',
      key: 'pluginCode',
      render: (_v, r) => r.pluginCode || String(r.pluginId),
    },
    {
      title: '当前版本',
      key: 'version',
      render: (_v, r) => {
        const ver = versionByKey.get(versionKey(r.serviceId, r.pluginId));
        return ver ? <Tag color="blue">{ver}</Tag> : <Tag>未发布</Tag>;
      },
    },
    {
      title: '操作',
      key: 'option',
      render: (_v, r) => (
        <Space size="middle">
          <a key="version" onClick={() => openVersionDrawer(r)}>
            版本历史 / 改版本
          </a>
          <Popconfirm
            key="unbind"
            title="确认解绑?"
            description="解绑后下次重启不再向该服务分发此插件。"
            okText="解绑"
            cancelText="取消"
            onConfirm={() => handleUnbind(r)}
          >
            <a style={{ color: '#ff4d4f' }}>解绑</a>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // 版本历史抽屉列:版本 / 版本序号 / 发布时间 / 状态 / 操作(重新激活=改版本)。
  const historyColumns: ColumnsType<ReleaseRow> = [
    { title: '版本', dataIndex: 'version', key: 'version', render: (_v, r) => r.version || '-' },
    { title: '版本序号', dataIndex: 'versionOrder', key: 'versionOrder' },
    {
      title: '发布时间',
      dataIndex: 'publishTime',
      key: 'publishTime',
      render: (_v, r) => r.publishTime || '-',
    },
    {
      title: '状态',
      key: 'status',
      render: (_v, r) => (
        <Space size={4}>
          {r.isActive ? <Tag color="green">运行中</Tag> : <Tag>历史</Tag>}
          {r.isRolledBack ? <Tag color="orange">已回滚</Tag> : null}
        </Space>
      ),
    },
    {
      title: '操作',
      key: 'option',
      render: (_v, r) =>
        // active 行无需切换,置灰显「当前」;非 active 行可「重新激活」=改到该版本。
        r.isActive ? (
          <span style={{ color: 'rgba(0,0,0,0.25)' }}>当前</span>
        ) : (
          <Button
            type="link"
            size="small"
            loading={reactivatingId === r.id}
            // 任一行重新激活提交中时,禁用其余行,避免并发切换。
            disabled={reactivatingId !== null && reactivatingId !== r.id}
            onClick={() => handleReactivate(r)}
          >
            切到此版本
          </Button>
        ),
    },
  ];

  // 服务下拉初次加载中:整页居中 Spin(对齐 lazyPage fallback 风格)。
  if (svcLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}>
        <Spin />
      </div>
    );
  }

  // 服务下拉加载失败:页面级重试(不静默卡空白)。
  if (svcErrored) {
    return (
      <>
        {contextHolder}
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
      </>
    );
  }

  return (
    <>
      {contextHolder}

      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {/* 顶部:选服务 + 说明。选定服务后才展示其绑定配置。 */}
        <Card>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Space wrap>
              <Text strong>服务:</Text>
              <Select
                showSearch
                allowClear
                optionFilterProp="label"
                style={{ minWidth: 320 }}
                placeholder="请选择要配置的服务"
                options={serviceSelectOptions}
                value={serviceId}
                onChange={(v) => setServiceId(v)}
                notFoundContent={serviceOptions.length ? undefined : '暂无服务,请先在「服务」页创建'}
              />
            </Space>
            <Text type="secondary">
              「绑定 / 解绑」决定该服务装哪些插件(服务级,无需选实例);「改版本」切换某插件的运行版本。均下次重启生效。
            </Text>
          </Space>
        </Card>

        {/* 未选服务:占位引导。 */}
        {serviceId === undefined ? (
          <Card>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="请选择一个服务以配置其插件"
            />
          </Card>
        ) : (
          <Card
            title={
              <Space>
                <ApiOutlined />
                <span>服务配置</span>
                {selectedService ? (
                  <Tag color="blue">
                    {selectedService.name
                      ? `${selectedService.serviceCode}(${selectedService.name})`
                      : selectedService.serviceCode}
                  </Tag>
                ) : null}
              </Space>
            }
            extra={
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => void openBind()}
                disabled={bindingsLoading || bindingsErrored}
              >
                绑定插件
              </Button>
            }
          >
            {bindingsLoading ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: 32 }}>
                <Spin />
              </div>
            ) : bindingsErrored ? (
              <Alert
                type="error"
                showIcon
                message="加载该服务的插件绑定失败"
                description="请稍后重试。"
                action={
                  <Button size="small" onClick={() => void fetchBindings(serviceId)}>
                    重试
                  </Button>
                }
              />
            ) : bindings.length ? (
              <Table<ServicePluginRow>
                rowKey={(r) => String(r.id)}
                columns={bindingColumns}
                dataSource={bindings}
                pagination={false}
                size="middle"
              />
            ) : (
              <Empty description="该服务暂未绑定插件 —— 点右上「绑定插件」" />
            )}
          </Card>
        )}
      </Space>

      {/* 绑定插件弹窗:从未绑定的插件里选一个绑定。 */}
      <Modal
        title="绑定插件"
        open={bindOpen}
        onCancel={() => setBindOpen(false)}
        onOk={() => void handleBind()}
        okText="绑定"
        cancelText="取消"
        okButtonProps={{ loading: binding, disabled: pluginToBind === undefined }}
        destroyOnClose
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Text type="secondary">为该服务绑定一个插件(已绑定的不在列表)。</Text>
          <Form layout="vertical">
            <Form.Item label="选择插件" required style={{ marginBottom: 0 }}>
              <Select
                showSearch
                optionFilterProp="label"
                style={{ width: '100%' }}
                placeholder="请选择插件"
                options={bindablePluginOptions}
                value={pluginToBind}
                onChange={(v) => setPluginToBind(v)}
                notFoundContent={
                  pluginOptions.length ? '该服务已绑定全部可用插件' : '暂无可绑定插件'
                }
              />
            </Form.Item>
          </Form>
        </Space>
      </Modal>

      {/* 改版本 / 版本历史抽屉:列该绑定全部已发布版本,非 active 行可「切到此版本」(重新激活)。 */}
      <Drawer
        title={
          versionDrawer
            ? `版本历史 / 改版本 — ${versionDrawer.pluginCode || versionDrawer.pluginId}`
            : '版本历史 / 改版本'
        }
        width={720}
        open={versionDrawer !== null}
        onClose={closeVersionDrawer}
        destroyOnClose
      >
        {versionDrawer && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Alert
              type="info"
              showIcon
              message="「切到此版本」把该插件的运行版本切到所选历史版本(下次重启生效)。发布全新版本请到「插件发布」页(需先上传版本)。"
            />
            {historyLoading ? (
              <div style={{ display: 'flex', justifyContent: 'center', padding: 32 }}>
                <Spin />
              </div>
            ) : historyErrored ? (
              <Alert
                type="error"
                showIcon
                message="加载版本历史失败"
                description="请稍后重试。"
                action={
                  <Button size="small" onClick={() => void fetchHistory(versionDrawer)}>
                    重试
                  </Button>
                }
              />
            ) : history.length ? (
              <Table<ReleaseRow>
                rowKey={(r) => String(r.id)}
                columns={historyColumns}
                dataSource={history}
                pagination={false}
                size="middle"
              />
            ) : (
              <Empty description="该插件暂无已发布版本 —— 请到「插件发布」页发布" />
            )}
          </Space>
        )}
      </Drawer>
    </>
  );
}
