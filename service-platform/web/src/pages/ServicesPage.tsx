import type { ProColumns, ProFormColumnsType } from '@ant-design/pro-components';
import CrudTable from '../components/CrudTable';
import * as resources from '../api/resources';

// 服务行记录(对齐 P1a 契约,全 camelCase)。
// - namespaceCode:后端 LEFT JOIN 回的命名空间可读名(列直接用,不客户端拼 id→名)。
// - serviceCode:服务编码;name:服务别名;dir:目录;nacosServiceName:Nacos 服务名(滚动部署用)。
interface ServiceRow {
  id: string | number;
  namespaceCode?: string;
  serviceCode: string;
  name?: string;
  dir?: string;
  nacosServiceName?: string;
}

// 命名空间下拉行(关联选择 options 用,取 code 作展示标签)。
interface NamespaceOption {
  id: string | number;
  code: string;
  name?: string;
}

// 列(对照基线 §2):namespaceCode(JOIN 名) / serviceCode / name / dir / nacosServiceName。
const columns: ProColumns<ServiceRow>[] = [
  { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode' },
  { title: '服务编码', dataIndex: 'serviceCode', key: 'serviceCode', copyable: true },
  { title: '服务别名', dataIndex: 'name', key: 'name' },
  { title: '目录', dataIndex: 'dir', key: 'dir' },
  { title: 'Nacos 服务名', dataIndex: 'nacosServiceName', key: 'nacosServiceName' },
];

// 命名空间关联选择 options:拉 list('namespaces'),value=id、label=code(name 兜底)。
// 一次取较大 pageSize 覆盖常规规模;选项即时拉取,不缓存(命名空间低频新增)。
const fetchNamespaceOptions = async () => {
  const env = await resources.list<NamespaceOption>('namespaces', { pageSize: 100 });
  return env.rows.map((n) => ({ label: n.code || String(n.id), value: n.id }));
};

// 表单字段(对照基线 §2):namespaceId(必填,关联选择)/ serviceCode(必填)/ name / dir
// / defaultImage(旧 image → defaultImage)/ nacosServiceName(新增)。
const formFields: ProFormColumnsType<ServiceRow>[] = [
  {
    title: '命名空间',
    dataIndex: 'namespaceId',
    valueType: 'select',
    // 关联选择:选项来自服务端 list('namespaces'),value 为 id。
    request: fetchNamespaceOptions,
    fieldProps: { showSearch: true, placeholder: '请选择命名空间' },
    formItemProps: { rules: [{ required: true, message: '请选择命名空间' }] },
  },
  {
    title: '服务编码',
    dataIndex: 'serviceCode',
    formItemProps: { rules: [{ required: true, message: '请输入服务编码' }] },
  },
  { title: '服务别名', dataIndex: 'name' },
  { title: '目录', dataIndex: 'dir' },
  { title: '默认镜像', dataIndex: 'defaultImage' },
  { title: 'Nacos 服务名', dataIndex: 'nacosServiceName' },
];

/**
 * 服务页(resource `services`):标准 CRUD。
 * - 列用后端 JOIN 回的 `namespaceCode`,不客户端拼 id→名(基线 §2)。
 * - 表单 `namespaceId` 关联选择,选项来自 `list('namespaces')`;唯一冲突 409 由 CrudTable 统一提示。
 * - P2 不做:命令下发 / 重启服务 / 命令历史(基线 §2「P2 故意不做」)。
 */
export default function ServicesPage() {
  return (
    <CrudTable<ServiceRow>
      resource="services"
      title="服务"
      columns={columns}
      formFields={formFields}
    />
  );
}
