import type { ProColumns, ProFormColumnsType } from '@ant-design/pro-components';
import CrudTable from '../components/CrudTable';
import * as resources from '../api/resources';

// 服务插件(服务 ↔ 插件绑定)行记录(对齐 P1a 契约,全 camelCase)。
// 列全部用后端 LEFT JOIN 回的可读名,不客户端拼 id→名(基线 §4)。
// ⚠️ P1a service-plugins list 关联回 serviceCode(label service_code),**不存在 serviceName**;
// serviceName 仅作 render 兜底(防未来后端改回),实际取 serviceCode。
interface ServicePluginRow {
  id: string | number;
  namespaceCode?: string;
  serviceCode?: string;
  serviceName?: string;
  pluginCode?: string;
}

// 关联选择 options 行最小约束。
interface NamespaceOption {
  id: string | number;
  code: string;
}
interface ServiceOption {
  id: string | number;
  serviceCode: string;
  name?: string;
}
interface PluginOption {
  id: string | number;
  code: string;
}

// 命名空间选项:list('namespaces'),label=code、value=id。
// B4:pageSize 取后端硬上限 200(各 list 端点 Query le=200,前后端必须一致;取 500 会 422 下拉崩)
// + 下拉 showSearch 本地过滤可检索。>200 的真·大集合需远程搜索,属后续增强。
const NS_OPTIONS_PAGE_SIZE = 200;
const fetchNamespaceOptions = async () => {
  const env = await resources.list<NamespaceOption>('namespaces', { pageSize: NS_OPTIONS_PAGE_SIZE });
  return env.rows.map((n) => ({ label: n.code || String(n.id), value: n.id }));
};

// B2 筛选区服务选项:拉全量服务(不带 namespaceId),label=serviceCode(name 后缀),value=id。
// B4:下拉 showSearch + 抬高 pageSize 上限,大集合可检索。
const fetchAllServiceOptions = async () => {
  const env = await resources.list<ServiceOption>('services', { pageSize: NS_OPTIONS_PAGE_SIZE });
  return env.rows.map((s) => ({
    label: s.name ? `${s.serviceCode}(${s.name})` : s.serviceCode,
    value: s.id,
  }));
};

// 列(对照基线 §4):namespaceCode / serviceCode / pluginCode(均后端 JOIN 可读名)。
// B2:展示列 `search: false`(JOIN 名非后端过滤键);单加一个仅查询用的服务筛选列,
// 经 search.transform 透传后端 ?serviceId=(service-plugins 仅支持按 serviceId 过滤)。
const columns: ProColumns<ServicePluginRow>[] = [
  // B2 筛选列:仅查询表单(hideInTable)。dataIndex=filterServiceId 避开下方表单 serviceId 撞 id,
  // transform 映射回后端过滤键 serviceId;选项来自全量 services,B4 showSearch 可检索。
  {
    title: '服务',
    dataIndex: 'filterServiceId',
    key: 'filterServiceId',
    hideInTable: true,
    valueType: 'select',
    request: fetchAllServiceOptions,
    fieldProps: { showSearch: true, placeholder: '按服务筛选' },
    search: { transform: (value) => ({ serviceId: value }) },
  },
  { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode', search: false },
  {
    title: '服务',
    // P1a 实际回 serviceCode(非 serviceName);dataIndex 对齐真实字段,render 兜底防未来后端改回 serviceName。
    dataIndex: 'serviceCode',
    key: 'serviceCode',
    search: false,
    render: (_dom, r) => r.serviceName || r.serviceCode || '-',
  },
  { title: '插件', dataIndex: 'pluginCode', key: 'pluginCode', search: false },
];

/**
 * 服务插件页(resource `service-plugins`):服务 ↔ 插件绑定,仅 列表/添加/删除(无编辑)。
 *
 * 三级级联(命名空间 → 服务 → 插件)**走服务端过滤**(基线 §4 重点):
 * - 选命名空间后,服务下拉调 `list('services', { namespaceId })`(带 `?namespaceId=` 服务端参数),
 *   **不**纯客户端拉全量再 filter —— 手写 ProForm 无 NocoBase 关联选择那种自动 filter magic,
 *   必须显式把过滤参数透传到服务端。
 * - 选服务后,插件下拉调 `list('plugins')`。
 * - 依赖项变化(`dependencies`)时 ProForm 自动重跑对应字段的 request,实现联动。
 *
 * B2 筛选:`searchable` 开查询表单,按 `serviceId` 服务端过滤(后端 service-plugins 仅支持 ?serviceId=)。
 * B3:无 code 字段,409 = 重复绑定 → conflictMessage「该插件已绑定该服务,请勿重复关联」(非「编码已存在」)。
 * 无编辑:CrudTable `editable={false}`,仅增删。
 */
export default function ServicePluginsPage() {
  // 表单字段(三级级联,对照基线 §4)。放在组件内:dependencies 的 request 闭包语义更清晰。
  const formFields: ProFormColumnsType<ServicePluginRow>[] = [
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
      // 依赖命名空间:namespaceId 变化时 ProForm 重跑本 request,并把其值带进 params。
      dependencies: ['namespaceId'],
      // 服务端过滤:必须带 ?namespaceId=,未选命名空间则不拉(返回空)。
      request: async (params) => {
        const namespaceId = (params as { namespaceId?: string | number }).namespaceId;
        if (namespaceId === undefined || namespaceId === null || namespaceId === '') return [];
        const env = await resources.list<ServiceOption>('services', {
          namespaceId,
          pageSize: NS_OPTIONS_PAGE_SIZE,
        });
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
      // 依赖服务:选服务后再拉插件(选服务前不拉)。
      dependencies: ['serviceId'],
      request: async (params) => {
        const serviceId = (params as { serviceId?: string | number }).serviceId;
        if (serviceId === undefined || serviceId === null || serviceId === '') return [];
        const env = await resources.list<PluginOption>('plugins', { pageSize: NS_OPTIONS_PAGE_SIZE });
        return env.rows.map((p) => ({ label: p.code || String(p.id), value: p.id }));
      },
      fieldProps: { showSearch: true, placeholder: '请先选择服务' },
      formItemProps: { rules: [{ required: true, message: '请选择插件' }] },
    },
  ];

  return (
    <CrudTable<ServicePluginRow>
      resource="service-plugins"
      title="服务插件"
      columns={columns}
      formFields={formFields}
      // 关联绑定表只增删,无编辑(基线 §4「本页无编辑」)。
      editable={false}
      // B2:开查询表单(按服务筛选)。
      searchable
      // B3:无 code,409=重复绑定,文案须贴切(非默认「编码已存在」)。
      conflictMessage="该插件已绑定该服务,请勿重复关联"
      // A1:三级级联(命名空间→服务→插件)父级变更时清空下级,杜绝错配绑定。
      cascadeChildren={{
        namespaceId: ['serviceId', 'pluginId'],
        serviceId: ['pluginId'],
      }}
      // C3:namespaceId 仅用于级联拉服务选项,提交时裁掉,只发 {serviceId, pluginId}。
      transformValues={({ serviceId, pluginId }) => ({ serviceId, pluginId })}
    />
  );
}
