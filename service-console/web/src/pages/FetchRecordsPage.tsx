import { ProTable, type ProColumns } from '@ant-design/pro-components';
import * as resources from '../api/resources';
import { useNamespace } from '../context/NamespaceContext';

// 获取记录行(对齐 P1a `fetch-records` list 契约,全 camelCase)。
// 列全部用后端 LEFT JOIN 回的可读名(基线 §7):
//   namespaceCode / serviceCode / pluginCode / version / fetchDate / remark。
// ⚠️ P1a 关联回的是 serviceCode(label service_code),**不存在 serviceName**,服务列用 serviceCode。
interface FetchRecordRow {
  id: string | number;
  namespaceCode?: string;
  serviceCode?: string;
  pluginCode?: string;
  version?: string;
  /** 获取时间(后端回 ISO8601 字符串;列用 valueType 'dateTime' 经 dayjs 格式化展示)。 */
  fetchDate?: string;
  /** 备注(后端 FetchRecordOut.remark,可空)。 */
  remark?: string;
}

// 筛选下拉选项行最小约束。
interface NamespaceOption {
  id: string | number;
  code: string;
}
interface ServiceOption {
  id: string | number;
  serviceCode: string;
  name?: string;
}

// B4:筛选下拉 pageSize 取后端硬上限 200(各 list 端点 Query le=200,前后端必须一致;取 500 会 422)
// + showSearch 本地过滤,已加载选项可检索。>200 的真·大集合需远程搜索,属后续增强。
const OPTIONS_PAGE_SIZE = 200;

// 命名空间筛选选项:list('namespaces'),value=id、label=code。
const fetchNamespaceOptions = async () => {
  const env = await resources.list<NamespaceOption>('namespaces', { pageSize: OPTIONS_PAGE_SIZE });
  return env.rows.map((n) => ({ label: n.code || String(n.id), value: n.id }));
};

// 服务筛选选项:list('services'),value=id、label=serviceCode(name 后缀)。
const fetchServiceOptions = async () => {
  const env = await resources.list<ServiceOption>('services', { pageSize: OPTIONS_PAGE_SIZE });
  return env.rows.map((s) => ({
    label: s.name ? `${s.serviceCode}(${s.name})` : s.serviceCode,
    value: s.id,
  }));
};

// 列(对照基线 §7,纯只读):后端 JOIN 可读名 + 获取时间 + 备注。
// B2:展示列 `search: false`(JOIN 名非后端过滤键);单加 namespaceId/serviceId 两个仅查询用筛选列
// (`hideInTable` + select),dataIndex 直接对齐后端过滤参数(本页无表单,无 id 撞键之虞)。
const columns: ProColumns<FetchRecordRow>[] = [
  // B2 筛选列(命名空间 / 服务),仅出现在查询表单;B4 下拉 showSearch 可检索。
  {
    title: '命名空间',
    dataIndex: 'namespaceId',
    key: 'namespaceId',
    hideInTable: true,
    valueType: 'select',
    request: fetchNamespaceOptions,
    fieldProps: { showSearch: true, placeholder: '按命名空间筛选' },
  },
  {
    title: '服务',
    dataIndex: 'serviceId',
    key: 'serviceId',
    hideInTable: true,
    valueType: 'select',
    request: fetchServiceOptions,
    fieldProps: { showSearch: true, placeholder: '按服务筛选' },
  },
  { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode', search: false },
  // P1a 关联回 serviceCode(非 serviceName);服务列用 serviceCode。
  { title: '服务', dataIndex: 'serviceCode', key: 'serviceCode', search: false },
  { title: '插件', dataIndex: 'pluginCode', key: 'pluginCode', search: false },
  { title: '插件版本', dataIndex: 'version', key: 'version', search: false },
  // C1:后端回 ISO8601(带 T/Z),用 valueType 'dateTime' 经 dayjs 格式化,避免直显原始 ISO。
  { title: '获取时间', dataIndex: 'fetchDate', key: 'fetchDate', valueType: 'dateTime', search: false },
  // C2:备注列(后端 FetchRecordOut 有,基线 §7 列出),长文本省略。
  { title: '备注', dataIndex: 'remark', key: 'remark', search: false, ellipsis: true },
];

/**
 * 获取记录页(resource `fetch-records`,旧 t_fetch_records):**纯只读审计表**(基线 §7)。
 *
 * 记录由节点侧分发端点(P1a `GET /api/distribution/plugins`)拉取插件时写入,**非 UI 创建**,
 * 故本页无任何增删改动作(对照基线「本页无写操作」)。
 *
 * 此审计表行数无界,**必须服务端分页**:ProTable `request` 把 antd 的 `current`/`pageSize`
 * 映射为后端 `page`/`pageSize`,读统一信封 `{count, rows, …}` 后返回 ProTable 约定结构,
 * **勿全量返回**。B2:筛选(`?namespaceId=` / `?serviceId=`,基线硬要求)经查询表单的
 * namespaceId/serviceId 筛选列平铺透传到服务端。
 *
 * P3-10:顶栏命名空间切换器联动 —— 选具体 ns 时按其 id 强制注入 `?namespaceId=`(经 params 的
 * `globalNamespaceId`,在 request 内覆盖本页命名空间筛选列,以全局为准);选「全部」不注入,回到全集。
 */
export default function FetchRecordsPage() {
  // P3-10:全局命名空间(null = 全部 → 不过滤;具体 ns → 按其数值 id 过滤 ?namespaceId=)。
  const { namespace } = useNamespace();
  return (
    <ProTable<FetchRecordRow>
      rowKey="id"
      columns={columns}
      // P3-10:全局 ns 经 params 透传(独立键 globalNamespaceId,避免与表单 namespaceId 撞);变更即重拉。
      params={{ globalNamespaceId: namespace?.id ?? '' }}
      // 服务端分页:current/pageSize → 后端 page/pageSize;其余 params(查询表单产生的
      // namespaceId/serviceId 过滤)平铺透传;读信封后返回 ProTable 约定结构。
      request={async (params) => {
        const { current, pageSize, globalNamespaceId, ...filter } = params as typeof params & {
          globalNamespaceId?: string | number;
        };
        const cleaned: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(filter)) {
          if (v !== undefined && v !== null && v !== '') cleaned[k] = v;
        }
        // 全局命名空间优先:选了具体 ns 则以其 id 强制覆盖本页 namespaceId 筛选列(以全局为准)。
        if (globalNamespaceId !== undefined && globalNamespaceId !== null && globalNamespaceId !== '') {
          cleaned.namespaceId = globalNamespaceId;
        }
        const env = await resources.list<FetchRecordRow>('fetch-records', {
          page: current,
          pageSize,
          ...cleaned,
        });
        return { data: env.rows, total: env.count, success: true };
      }}
      pagination={{ showSizeChanger: true }}
      // B2:开查询表单(命名空间/服务筛选,基线硬要求)。只读页无添加/编辑/Drawer。
      search={{ labelWidth: 'auto', defaultCollapsed: false }}
      options={{ reload: true, density: false, setting: false }}
      toolBarRender={false}
      dateFormatter="string"
    />
  );
}
