import type { ProColumns, ProFormColumnsType } from '@ant-design/pro-components';
import CrudTable from '../components/CrudTable';

// 插件行记录(对齐 P1a 契约,全 camelCase)。
// - code:插件编码(主标签;旧 pluginCode → P1a `/api/plugins` 规范为 code)。
// - name:插件别名(可空)。
interface PluginRow {
  id: string | number;
  code: string;
  name?: string;
}

// 列(对照基线 §3):code / name。
const columns: ProColumns<PluginRow>[] = [
  { title: '插件编码', dataIndex: 'code', key: 'code', copyable: true },
  { title: '插件别名', dataIndex: 'name', key: 'name' },
];

// 表单字段(对照基线 §3):code 必填 + name 选填。
const formFields: ProFormColumnsType<PluginRow>[] = [
  {
    title: '插件编码',
    dataIndex: 'code',
    formItemProps: { rules: [{ required: true, message: '请输入插件编码' }] },
  },
  { title: '插件别名', dataIndex: 'name' },
];

/**
 * 插件页(resource `plugins`):标准 CRUD,纯字典维护。
 * - 唯一冲突 409 由 CrudTable 统一提示;无 P2 命令类动作(基线 §3)。
 */
export default function PluginsPage() {
  return (
    <CrudTable<PluginRow>
      resource="plugins"
      title="插件"
      columns={columns}
      formFields={formFields}
    />
  );
}
