import type { ProColumns, ProFormColumnsType } from '@ant-design/pro-components';
import CrudTable from '../components/CrudTable';
import * as resources from '../api/resources';
import { useNamespace } from '../context/NamespaceContext';

// 服务行记录(对齐 P1a 契约,全 camelCase)。
// - namespaceCode:后端 LEFT JOIN 回的命名空间可读名(列直接用,不客户端拼 id→名)。
// - namespaceId:命名空间 id(后端 ServiceOut 回;C5 编辑时回填关联选择必需)。
// - serviceCode:服务编码;name:服务别名;dir:目录;defaultImage:默认镜像;nacosServiceName:Nacos 服务名(滚动部署用)。
interface ServiceRow {
  id: string | number;
  namespaceId?: string | number;
  namespaceCode?: string;
  serviceCode: string;
  name?: string;
  dir?: string;
  defaultImage?: string;
  nacosServiceName?: string;
}

// 命名空间下拉行(关联选择 options 用,取 code 作展示标签)。
interface NamespaceOption {
  id: string | number;
  code: string;
  name?: string;
}

// 命名空间关联选择 options:拉 list('namespaces'),value=id、label=code(name 兜底)。
// B4:pageSize 取后端硬上限 200(各 list 端点 Query le=200,前后端必须一致;取 500 会 422 下拉崩)
// + 下拉 showSearch 本地过滤,已加载选项可检索。>200 的真·大集合需远程搜索,属后续增强。
// 选项即时拉取,不缓存(命名空间低频新增)。
const NS_OPTIONS_PAGE_SIZE = 200;
const fetchNamespaceOptions = async () => {
  const env = await resources.list<NamespaceOption>('namespaces', { pageSize: NS_OPTIONS_PAGE_SIZE });
  return env.rows.map((n) => ({ label: n.code || String(n.id), value: n.id }));
};

// 列(对照基线 §2):namespaceCode(JOIN 名) / serviceCode / name / dir / nacosServiceName。
// B2:展示列均 `search: false`(JOIN 回的名不是后端过滤键);单独加一个仅查询用的 namespaceId
// 筛选列(`hideInTable` + select),其值经 ProTable params 透传到后端 ?namespaceId= 服务端过滤。
const columns: ProColumns<ServiceRow>[] = [
  // B2 筛选列:仅出现在查询表单(hideInTable)。dataIndex 用 `filterNamespaceId` 避免与下方表单的
  // namespaceId select 撞 DOM id;经 search.transform 把筛选值映射回后端过滤键 `namespaceId`。
  // 选项来自 list('namespaces'),B4 下拉 showSearch 可检索。
  {
    title: '命名空间',
    dataIndex: 'filterNamespaceId',
    key: 'filterNamespaceId',
    hideInTable: true,
    valueType: 'select',
    request: fetchNamespaceOptions,
    fieldProps: { showSearch: true, placeholder: '按命名空间筛选' },
    // 透传后端服务端过滤参数:?namespaceId=(对齐 list_services 的 alias)。
    search: { transform: (value) => ({ namespaceId: value }) },
  },
  { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode', search: false },
  { title: '服务编码', dataIndex: 'serviceCode', key: 'serviceCode', copyable: true, search: false },
  { title: '服务别名', dataIndex: 'name', key: 'name', search: false },
  { title: '目录', dataIndex: 'dir', key: 'dir', search: false },
  { title: 'Nacos 服务名', dataIndex: 'nacosServiceName', key: 'nacosServiceName', search: false },
];

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
 * - B2 筛选:`searchable` 开查询表单,按 `namespaceId` 服务端过滤(透传后端 ?namespaceId=)。
 * - 表单 `namespaceId` 关联选择,选项来自 `list('namespaces')`;C5 编辑回填靠后端 list 回的 namespaceId/dir/defaultImage。
 * - B3:409 = 命名空间内 service_code 重复 → conflictMessage「该命名空间下服务编码已存在」(非「编码已存在」)。
 * - P3-10:顶栏命名空间切换器联动 —— 选具体 ns 时按其 id 注入 `?namespaceId=` 服务端过滤(经 extraParams,
 *   覆盖本页命名空间筛选列,以全局为准);选「全部」则不注入,列表回到全集。
 * - P2 不做:命令下发 / 重启服务 / 命令历史(基线 §2「P2 故意不做」)。
 */
export default function ServicesPage() {
  // P3-10:全局命名空间(null = 全部 → 不过滤;具体 ns → 按其数值 id 过滤 ?namespaceId=)。
  const { namespace } = useNamespace();
  return (
    <CrudTable<ServiceRow>
      resource="services"
      title="服务"
      columns={columns}
      formFields={formFields}
      // B2:开查询表单(命名空间筛选列)。
      searchable
      // B3:服务 409 是命名空间内 service_code 重复,文案须贴切(非默认「编码已存在」)。
      conflictMessage="该命名空间下服务编码已存在"
      // P3-10:全局 ns 注入服务端过滤 —— 仅选了具体 ns 才传(此时覆盖本页筛选列,以全局为准);
      // 「全部」则不传(prop=undefined),本页命名空间筛选列照常可用,二者不打架。
      extraParams={namespace ? { namespaceId: namespace.id } : undefined}
    />
  );
}
