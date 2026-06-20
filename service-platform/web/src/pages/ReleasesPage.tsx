import { useRef, useState } from 'react';
import {
  ProTable,
  DrawerForm,
  BetaSchemaForm,
  type ActionType,
  type ProColumns,
  type ProFormColumnsType,
} from '@ant-design/pro-components';
import { Button, Drawer, Popconfirm, Space, Tag, message } from 'antd';
import { CloudUploadOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';

// 发布行记录(对齐 P1a `releases` list 契约,全 camelCase)。
// 列全部用后端 LEFT JOIN 回的可读名(namespaceCode/serviceName/pluginCode/version),不客户端拼 id→名。
// isActive / isRolledBack:enum 'yes' | 'no'(基线 §6 / §10),用 Tag 标色。
// serviceId / pluginId:行操作(历史版本 / 回滚)定位用;主表行即「当前 active 行」。
interface ReleaseRow {
  id: string | number;
  namespaceCode?: string;
  serviceName?: string;
  serviceCode?: string;
  pluginCode?: string;
  version?: string;
  publishTime?: string;
  isActive?: 'yes' | 'no';
  isRolledBack?: 'yes' | 'no';
  versionOrder?: number;
  serviceId: string | number;
  pluginId: string | number;
}

// 关联选择 options 行最小约束(级联各级)。
interface NamespaceOption {
  id: string | number;
  code: string;
}
interface ServiceOption {
  id: string | number;
  serviceCode: string;
  name?: string;
}
// service-plugins 绑定行:取该服务已绑定的插件(列用后端 JOIN 回的 pluginCode/pluginId)。
interface ServicePluginOption {
  id: string | number;
  pluginId: string | number;
  pluginCode?: string;
}
interface PluginVersionOption {
  id: string | number;
  version?: string;
}

// 运行版本 Tag(isActive=yes → 绿色「运行中」,否则灰「历史」)。
const renderActiveTag = (isActive?: 'yes' | 'no') =>
  isActive === 'yes' ? <Tag color="green">运行中</Tag> : <Tag>历史</Tag>;

// 是否回滚过 Tag(isRolledBack=yes → 橙色「已回滚」,否则不标色显 '-')。
const renderRolledBackTag = (isRolledBack?: 'yes' | 'no') =>
  isRolledBack === 'yes' ? <Tag color="orange">已回滚</Tag> : <span>-</span>;

// 命名空间选项:list('namespaces'),label=code、value=id。
const fetchNamespaceOptions = async () => {
  const env = await resources.list<NamespaceOption>('namespaces', { pageSize: 100 });
  return env.rows.map((n) => ({ label: n.code || String(n.id), value: n.id }));
};

/**
 * 插件发布页(resource `releases`,旧 t_service_plugin_version):发布 + 历史版本 + 重新激活 + 回滚。
 * 全页动作最密集(对照基线 §6)。
 *
 * 主表(`listReleases()` **不传 filter**):后端按 isActive=yes 回每个 service+plugin 绑定当前激活行,
 * 列直接用后端 LEFT JOIN 回的可读名;服务端分页。isActive/isRolledBack 用 Tag 标色。
 *
 * 工具条「发布」开 Drawer:**四级级联** 命名空间→服务→插件→版本,**逐级服务端过滤**:
 *  - 服务   list('services', { namespaceId })       —— 带 ?namespaceId=
 *  - 插件   list('service-plugins', { serviceId })   —— 带 ?serviceId=(取该服务已绑定插件,基线 §6)
 *  - 版本   listPluginVersions({ pluginId })         —— 带 ?pluginId=
 *  选满 → publish({ serviceId, pluginId, pluginVersionId })。
 *
 * 行操作:
 *  - 「历史版本」:抽屉调 listReleaseHistory({ serviceId, pluginId }) 列该绑定全部 spv 历史,
 *    每行可「重新激活」reactivate({ spvId })。
 *  - 「回滚」:rollback({ spvId })。
 *
 * P2 不做(基线 §6):发布并一键无感上线(滚动)、重启服务 / 下发命令 —— 不建对应按钮。
 */
export default function ReleasesPage() {
  const actionRef = useRef<ActionType>();
  const [messageApi, contextHolder] = message.useMessage();

  // 发布 Drawer 开关。
  const [publishOpen, setPublishOpen] = useState(false);

  // 历史版本抽屉:记录当前查看的绑定(serviceId+pluginId),为空表示关闭。
  // 含可读名仅用于抽屉标题展示;过滤只用 serviceId+pluginId。
  const [history, setHistory] = useState<{
    serviceId: string | number;
    pluginId: string | number;
    serviceName?: string;
    pluginCode?: string;
  } | null>(null);
  const historyActionRef = useRef<ActionType>();

  const reloadMain = () => actionRef.current?.reload();

  // 主表列(对照基线 §6):namespaceCode / serviceName / pluginCode / version / publishTime + 两个 Tag 列。
  const columns: ProColumns<ReleaseRow>[] = [
    { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode' },
    {
      title: '服务',
      // P1a 实际回 serviceCode(非 serviceName);dataIndex 对齐真实字段,render 兜底防未来后端改回 serviceName。
      dataIndex: 'serviceCode',
      key: 'serviceCode',
      render: (_dom, r) => r.serviceName || r.serviceCode || '-',
    },
    { title: '插件', dataIndex: 'pluginCode', key: 'pluginCode' },
    { title: '当前版本', dataIndex: 'version', key: 'version' },
    { title: '发布时间', dataIndex: 'publishTime', key: 'publishTime' },
    {
      title: '运行版本',
      dataIndex: 'isActive',
      key: 'isActive',
      render: (_dom, r) => renderActiveTag(r.isActive),
    },
    {
      title: '是否回滚过',
      dataIndex: 'isRolledBack',
      key: 'isRolledBack',
      render: (_dom, r) => renderRolledBackTag(r.isRolledBack),
    },
    {
      title: '操作',
      valueType: 'option',
      key: 'option',
      render: (_dom, record) => (
        <Space size="middle">
          <a key="history" onClick={() => setHistory(record)}>
            历史版本
          </a>
          <Popconfirm
            key="rollback"
            title="确认回滚到上一未回滚版本?"
            okText="回滚"
            cancelText="取消"
            onConfirm={() => handleRollback(record)}
          >
            <a>回滚</a>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // 历史抽屉列(对照基线 §6):version / versionOrder / publishTime + 两 Tag,行内「重新激活」。
  const historyColumns: ProColumns<ReleaseRow>[] = [
    { title: '版本', dataIndex: 'version', key: 'version' },
    { title: '版本序号', dataIndex: 'versionOrder', key: 'versionOrder' },
    { title: '发布时间', dataIndex: 'publishTime', key: 'publishTime' },
    {
      title: '运行版本',
      dataIndex: 'isActive',
      key: 'isActive',
      render: (_dom, r) => renderActiveTag(r.isActive),
    },
    {
      title: '是否回滚过',
      dataIndex: 'isRolledBack',
      key: 'isRolledBack',
      render: (_dom, r) => renderRolledBackTag(r.isRolledBack),
    },
    {
      title: '操作',
      valueType: 'option',
      key: 'option',
      render: (_dom, record) =>
        // 已是运行版本则无需重新激活,置灰提示。
        record.isActive === 'yes' ? (
          <span style={{ color: 'rgba(0,0,0,0.25)' }}>当前运行</span>
        ) : (
          <a key="reactivate" onClick={() => handleReactivate(record)}>
            重新激活
          </a>
        ),
    },
  ];

  // 发布 Drawer 表单字段(四级级联,逐级服务端过滤;对照基线 §6)。
  const publishFields: ProFormColumnsType<Record<string, unknown>>[] = [
    {
      title: '命名空间',
      dataIndex: 'namespaceId',
      valueType: 'select',
      request: fetchNamespaceOptions,
      fieldProps: { showSearch: true, placeholder: '请选择命名空间' },
      formItemProps: { rules: [{ required: true, message: '请选择命名空间' }] },
    },
    {
      title: '服务',
      dataIndex: 'serviceId',
      valueType: 'select',
      // 依赖命名空间:服务端过滤 list('services', { namespaceId });未选则不拉。
      dependencies: ['namespaceId'],
      request: async (params) => {
        const namespaceId = (params as { namespaceId?: string | number }).namespaceId;
        if (namespaceId === undefined || namespaceId === null || namespaceId === '') return [];
        const env = await resources.list<ServiceOption>('services', { namespaceId, pageSize: 100 });
        return env.rows.map((s) => ({
          label: s.name ? `${s.serviceCode}(${s.name})` : s.serviceCode,
          value: s.id,
        }));
      },
      fieldProps: { showSearch: true, placeholder: '请先选择命名空间' },
      formItemProps: { rules: [{ required: true, message: '请选择服务' }] },
    },
    {
      title: '插件',
      dataIndex: 'pluginId',
      valueType: 'select',
      // 依赖服务:取该服务已绑定的插件 —— list('service-plugins', { serviceId })(基线 §6)。
      // value 用绑定行回的 pluginId(非 service-plugin 自身 id)。
      dependencies: ['serviceId'],
      request: async (params) => {
        const serviceId = (params as { serviceId?: string | number }).serviceId;
        if (serviceId === undefined || serviceId === null || serviceId === '') return [];
        const env = await resources.list<ServicePluginOption>('service-plugins', {
          serviceId,
          pageSize: 100,
        });
        return env.rows.map((sp) => ({
          label: sp.pluginCode || String(sp.pluginId),
          value: sp.pluginId,
        }));
      },
      fieldProps: { showSearch: true, placeholder: '请先选择服务' },
      formItemProps: { rules: [{ required: true, message: '请选择插件' }] },
    },
    {
      title: '版本',
      dataIndex: 'pluginVersionId',
      valueType: 'select',
      // 依赖插件:服务端过滤 listPluginVersions({ pluginId });未选则不拉。
      dependencies: ['pluginId'],
      request: async (params) => {
        const pluginId = (params as { pluginId?: string | number }).pluginId;
        if (pluginId === undefined || pluginId === null || pluginId === '') return [];
        const env = await resources.listPluginVersions<PluginVersionOption>({
          pluginId,
          pageSize: 100,
        });
        return env.rows.map((v) => ({ label: v.version || String(v.id), value: v.id }));
      },
      fieldProps: { showSearch: true, placeholder: '请先选择插件' },
      formItemProps: { rules: [{ required: true, message: '请选择版本' }] },
    },
  ];

  // 发布提交:取四级级联选中的 serviceId/pluginId/pluginVersionId 调 publish。
  const handlePublish = async (values: Record<string, unknown>): Promise<boolean> => {
    try {
      await resources.publish({
        serviceId: values.serviceId as string | number,
        pluginId: values.pluginId as string | number,
        pluginVersionId: values.pluginVersionId as string | number,
      });
      messageApi.success('发布成功');
      reloadMain();
      return true; // DrawerForm 返回 true 自动关闭
    } catch (e) {
      // 409 视为「该版本已发布过」,提示去历史版本重新激活;其余交回拦截器兜底。
      const status =
        typeof e === 'object' && e && 'response' in e
          ? (e as { response?: { status?: number } }).response?.status
          : undefined;
      if (status === 409) messageApi.error('该版本已发布过,请到「历史版本」重新激活');
      return false; // 不关闭抽屉,便于用户改选
    }
  };

  // 回滚:rollback({ spvId })(spvId = 当前 active 行 id);成功刷新主表 + 历史抽屉。
  const handleRollback = async (record: ReleaseRow) => {
    try {
      await resources.rollback({ spvId: record.id });
      messageApi.success('回滚成功');
      reloadMain();
      historyActionRef.current?.reload();
    } catch {
      // 失败(如非 active 版「无需回滚」)由 client 拦截器统一提示。
    }
  };

  // 重新激活:reactivate({ spvId })(spvId = 历史抽屉某行 id);成功刷新主表 + 历史抽屉。
  const handleReactivate = async (record: ReleaseRow) => {
    try {
      await resources.reactivate({ spvId: record.id });
      messageApi.success('重新激活成功');
      reloadMain();
      historyActionRef.current?.reload();
    } catch {
      // 失败由 client 拦截器统一提示。
    }
  };

  return (
    <>
      {contextHolder}
      <ProTable<ReleaseRow>
        actionRef={actionRef}
        rowKey="id"
        columns={columns}
        // 主表服务端分页;**不传 filter**(后端按 isActive=yes 回每绑定当前激活行)。
        // 仅透传分页参数,不把列 search 的 filter 拼进去(主表是聚合视图)。
        request={async (params) => {
          const { current, pageSize } = params;
          const env = await resources.listReleases<ReleaseRow>({ page: current, pageSize });
          return { data: env.rows, total: env.count, success: true };
        }}
        pagination={{ showSizeChanger: true }}
        search={false}
        options={{ reload: true, density: false, setting: false }}
        toolBarRender={() => [
          <Button
            key="publish"
            type="primary"
            icon={<CloudUploadOutlined />}
            onClick={() => setPublishOpen(true)}
          >
            发布
          </Button>,
        ]}
        dateFormatter="string"
      />

      {/* 发布 Drawer:四级级联 → publish。 */}
      <DrawerForm<Record<string, unknown>>
        title="发布插件版本"
        open={publishOpen}
        onOpenChange={setPublishOpen}
        onFinish={handlePublish}
        drawerProps={{ destroyOnClose: true }}
      >
        <BetaSchemaForm<Record<string, unknown>>
          layoutType="Embed"
          columns={publishFields as ProFormColumnsType<Record<string, unknown>>[]}
        />
      </DrawerForm>

      {/* 历史版本抽屉:listReleaseHistory({ serviceId, pluginId }) + 行内重新激活。 */}
      <Drawer
        title={
          history
            ? `历史版本 — ${history.serviceName || history.serviceId} / ${
                history.pluginCode || history.pluginId
              }`
            : '历史版本'
        }
        width={720}
        open={history !== null}
        onClose={() => setHistory(null)}
        destroyOnClose
      >
        {history && (
          <ProTable<ReleaseRow>
            actionRef={historyActionRef}
            rowKey="id"
            columns={historyColumns}
            // 历史抽屉:**按 serviceId+pluginId 服务端过滤**取该绑定全部 spv 历史(服务端分页)。
            request={async (params) => {
              const { current, pageSize } = params;
              const env = await resources.listReleaseHistory<ReleaseRow>({
                serviceId: history.serviceId,
                pluginId: history.pluginId,
                page: current,
                pageSize,
              });
              return { data: env.rows, total: env.count, success: true };
            }}
            pagination={{ showSizeChanger: true }}
            search={false}
            options={false}
            toolBarRender={false}
            dateFormatter="string"
          />
        )}
      </Drawer>
    </>
  );
}
