import { ProTable, type ProColumns } from '@ant-design/pro-components';
import * as resources from '../api/resources';

// 获取记录行(对齐 P1a `fetch-records` list 契约,全 camelCase)。
// 列全部用后端 LEFT JOIN 回的可读名(基线 §7):
//   namespaceCode / serviceCode / pluginCode / version / fetchDate。
// ⚠️ P1a 关联回的是 serviceCode(label service_code),**不存在 serviceName**,服务列用 serviceCode。
interface FetchRecordRow {
  id: string | number;
  namespaceCode?: string;
  serviceCode?: string;
  pluginCode?: string;
  version?: string;
  /** 获取时间(后端回 ISO 字符串,直接展示)。 */
  fetchDate?: string;
}

// 列(对照基线 §7,纯只读):后端 JOIN 可读名 + 获取时间。
const columns: ProColumns<FetchRecordRow>[] = [
  { title: '命名空间', dataIndex: 'namespaceCode', key: 'namespaceCode' },
  // P1a 关联回 serviceCode(非 serviceName);服务列用 serviceCode。
  { title: '服务', dataIndex: 'serviceCode', key: 'serviceCode' },
  { title: '插件', dataIndex: 'pluginCode', key: 'pluginCode' },
  { title: '插件版本', dataIndex: 'version', key: 'version' },
  { title: '获取时间', dataIndex: 'fetchDate', key: 'fetchDate' },
];

/**
 * 获取记录页(resource `fetch-records`,旧 t_fetch_records):**纯只读审计表**(基线 §7)。
 *
 * 记录由节点侧分发端点(P1a `GET /api/distribution/plugins`)拉取插件时写入,**非 UI 创建**,
 * 故本页无任何增删改动作(对照基线「本页无写操作」)。
 *
 * 此审计表行数无界,**必须服务端分页**:ProTable `request` 把 antd 的 `current`/`pageSize`
 * 映射为后端 `page`/`pageSize`,读统一信封 `{count, rows, …}` 后返回 ProTable 约定结构,
 * **勿全量返回**。可选筛选(`?namespaceId=` / `?serviceId=`)随列 search 平铺透传到服务端。
 */
export default function FetchRecordsPage() {
  return (
    <ProTable<FetchRecordRow>
      rowKey="id"
      columns={columns}
      // 服务端分页:current/pageSize → 后端 page/pageSize;其余 params(列 search 产生的
      // namespaceId/serviceId 过滤)平铺透传;读信封后返回 ProTable 约定结构。
      request={async (params) => {
        const { current, pageSize, ...filter } = params;
        const env = await resources.list<FetchRecordRow>('fetch-records', {
          page: current,
          pageSize,
          ...filter,
        });
        return { data: env.rows, total: env.count, success: true };
      }}
      pagination={{ showSizeChanger: true }}
      // 只读:无工具条添加按钮、无 Drawer 表单。仅保留刷新。
      search={false}
      options={{ reload: true, density: false, setting: false }}
      toolBarRender={false}
      dateFormatter="string"
    />
  );
}
