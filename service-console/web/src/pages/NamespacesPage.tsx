import { useState } from 'react';
import type { ProColumns, ProFormColumnsType } from '@ant-design/pro-components';
import CrudTable from '../components/CrudTable';
import ShowOnceModal from '../components/ShowOnceModal';
import * as resources from '../api/resources';

// 命名空间行记录(对齐 P1a 契约,全 camelCase)。
// - code:命名空间编码(主标签;旧 namespaceCode → P1a 规范为 code)。
// - name:命名空间别名(可空)。
// - online / lastHeartbeatAt:实时状态,P2 占位(列保留显 '-',P1 不接 hub)。
interface NamespaceRow {
  id: string | number;
  code: string;
  name?: string;
  online?: boolean;
  lastHeartbeatAt?: string;
}

// 列(对照基线 §1):code / name + 在线/最近心跳(P2 占位,统一显 '-')。
const columns: ProColumns<NamespaceRow>[] = [
  { title: '命名空间编码', dataIndex: 'code', key: 'code', copyable: true },
  { title: '命名空间别名', dataIndex: 'name', key: 'name' },
  // P2 占位:实时在线状态需接 hub 心跳,P1 不接,列保留但恒显 '-'。
  { title: '是否在线', dataIndex: 'online', key: 'online', search: false, render: () => '-' },
  {
    title: '最近心跳时间',
    dataIndex: 'lastHeartbeatAt',
    key: 'lastHeartbeatAt',
    search: false,
    render: () => '-',
  },
];

// 表单字段(对照基线 §1):code 必填 + name 选填。
// agentKey 不是输入字段:由后端 create 时签发、show-once 返回,前端只读弹窗展示。
const formFields: ProFormColumnsType<NamespaceRow>[] = [
  {
    title: '命名空间编码',
    dataIndex: 'code',
    formItemProps: { rules: [{ required: true, message: '请输入命名空间编码' }] },
  },
  { title: '命名空间别名', dataIndex: 'name' },
];

/**
 * 命名空间页(resource `namespaces`):CRUD + show-once 范例页。
 * - 创建成功若响应含 `agentKey` → ShowOnceModal 展示明文(P1a create namespace 返回 show-once agentKey)。
 * - 行操作扩展「轮换密钥」(rotate-key → {agentKey})、「轮换 pull token」(rotate-pull-token → {pullToken}),
 *   各调 API 后用同一个 ShowOnceModal 展示返回明文。
 */
export default function NamespacesPage() {
  // show-once 弹窗:复用一个弹窗承载 agentKey / pullToken 三种来源。
  const [secret, setSecret] = useState<{ title: string; value: string } | null>(null);

  const showSecret = (title: string, value: string) => setSecret({ title, value });

  const handleRotateKey = async (record: NamespaceRow) => {
    try {
      const { agentKey } = await resources.rotateKey(record.id);
      showSecret('Agent Key(轮换后)', agentKey);
    } catch {
      // 失败由 client 拦截器统一提示。
    }
  };

  const handleRotatePullToken = async (record: NamespaceRow) => {
    try {
      const { pullToken } = await resources.rotatePullToken(record.id);
      showSecret('Pull Token(轮换后)', pullToken);
    } catch {
      // 失败由 client 拦截器统一提示。
    }
  };

  return (
    <>
      <CrudTable<NamespaceRow>
        resource="namespaces"
        title="命名空间"
        columns={columns}
        formFields={formFields}
        onCreated={(created) => {
          // P1a create namespace 响应含 show-once agentKey 时弹窗展示。
          const agentKey = (created as { agentKey?: string } | null)?.agentKey;
          if (agentKey) showSecret('Agent Key', agentKey);
        }}
        rowExtraActions={(record) => (
          <>
            <a onClick={() => handleRotateKey(record)}>轮换密钥</a>
            <a onClick={() => handleRotatePullToken(record)} style={{ marginLeft: 16 }}>
              轮换 pull token
            </a>
          </>
        )}
      />
      <ShowOnceModal
        title={secret?.title ?? ''}
        value={secret?.value ?? ''}
        open={secret !== null}
        onClose={() => setSecret(null)}
      />
    </>
  );
}
