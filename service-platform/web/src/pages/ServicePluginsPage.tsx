import type { ProColumns, ProFormColumnsType } from '@ant-design/pro-components';
import CrudTable from '../components/CrudTable';
import * as resources from '../api/resources';

// 服务插件(服务 ↔ 插件绑定)行记录(对齐 P1a 契约,全 camelCase)。
// 列全部用后端 LEFT JOIN 回的可读名,不客户端拼 id→名(基线 §4)。
interface ServicePluginRow {
  id: string | number;
  namespaceCode?: string;
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

// 列(对照基线 §4):namespaceCode / serviceName / pluginCode(均后端 JOIN 可读名)。
const columns: ProColumns<ServicePluginRow>[] = [
  { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode' },
  { title: '服务', dataIndex: 'serviceName', key: 'serviceName' },
  { title: '插件', dataIndex: 'pluginCode', key: 'pluginCode' },
];

// 命名空间选项:list('namespaces'),label=code、value=id。
const fetchNamespaceOptions = async () => {
  const env = await resources.list<NamespaceOption>('namespaces', { pageSize: 100 });
  return env.rows.map((n) => ({ label: n.code || String(n.id), value: n.id }));
};

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
 * 唯一冲突 409 由 CrudTable 统一提示。无编辑:CrudTable `editable={false}`,仅增删。
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
      // 依赖服务:选服务后再拉插件(选服务前不拉)。
      dependencies: ['serviceId'],
      request: async (params) => {
        const serviceId = (params as { serviceId?: string | number }).serviceId;
        if (serviceId === undefined || serviceId === null || serviceId === '') return [];
        const env = await resources.list<PluginOption>('plugins', { pageSize: 100 });
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
    />
  );
}
