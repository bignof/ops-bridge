import { useRef } from 'react';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import { Card, Space, Typography, Upload, message, type UploadProps } from 'antd';
import { InboxOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';

// 已上传插件版本行(对照基线 §5,全 camelCase)。
// 列全部用后端 LEFT JOIN / attachment 回的可读名,不客户端拼 id→名。
interface PluginVersionRow {
  id: string | number;
  pluginCode?: string;
  version?: string;
  filename?: string;
}

// 插件筛选下拉选项行最小约束。
interface PluginOption {
  id: string | number;
  code: string;
}

// B4:筛选下拉一次取较大上限 + showSearch 本地过滤,大集合可检索。
const PLUGIN_OPTIONS_PAGE_SIZE = 500;
const fetchPluginOptions = async () => {
  const env = await resources.list<PluginOption>('plugins', { pageSize: PLUGIN_OPTIONS_PAGE_SIZE });
  return env.rows.map((p) => ({ label: p.code || String(p.id), value: p.id }));
};

// 列(对照基线 §5):pluginCode / version / filename(均后端回的可读名)。
// B2:展示列 `search: false`;单加一个仅查询用的 pluginId 筛选列(dataIndex 对齐后端 ?pluginId=,
// 本页无表单无 id 撞键之虞),其值经 ProTable params 透传到 listPluginVersions。
const columns: ProColumns<PluginVersionRow>[] = [
  // B2 筛选列:仅查询表单;B4 下拉 showSearch 可检索。
  {
    title: '插件',
    dataIndex: 'pluginId',
    key: 'pluginId',
    hideInTable: true,
    valueType: 'select',
    request: fetchPluginOptions,
    fieldProps: { showSearch: true, placeholder: '按插件筛选' },
  },
  { title: '插件编码', dataIndex: 'pluginCode', key: 'pluginCode', copyable: true, search: false },
  { title: '版本', dataIndex: 'version', key: 'version', search: false },
  { title: '文件名', dataIndex: 'filename', key: 'filename', ellipsis: true, search: false },
];

// 从 axios 异常中取 HTTP 状态码(无 response 时返回 undefined)。
const statusOf = (e: unknown): number | undefined =>
  typeof e === 'object' && e !== null && 'response' in e
    ? (e as { response?: { status?: number } }).response?.status
    : undefined;

/**
 * 插件上传页(resource `plugin-versions` 的 upload):
 * - 上方 antd Upload 拖拽 / 选 `.tgz` → `POST /api/plugin-versions/upload`(字段 file);
 *   成功后 message 回显后端解析出的 `version`(包内 package.json.version),并刷新下方列表。
 * - 下方 ProTable 服务端分页列出已上传版本(`GET /api/plugin-versions`,统一信封 `{count, rows}`);
 *   B2:查询表单按 `pluginId` 服务端过滤(透传后端 ?pluginId=)。
 *
 * 失败按 HTTP 状态码明确提示(读 `e.response.status`):
 * - 400 未匹配 / 匹配多个插件 → 「未匹配到插件…」
 * - 409 版本已存在            → 「该版本已存在」
 * - 413 文件超限              → 「文件超出大小限制」
 * - 其它                      → 通用「上传失败」(client 拦截器仅处理 401,此处兜底给可见提示)。
 *
 * 本页只负责上传 + 列版本,不建「发布」入口(发布统一在插件发布页,基线 §5 P2)。
 */
export default function PluginUploadPage() {
  const actionRef = useRef<ActionType>();
  const [messageApi, contextHolder] = message.useMessage();

  // 用 antd Upload 的 customRequest 接管上传:走资源层 uploadPluginVersion(而非默认 action URL),
  // 成功提示解析出的 version 并刷新列表;失败按状态码分类提示。
  const customRequest: UploadProps['customRequest'] = async (options) => {
    const { file, onSuccess, onError } = options;
    try {
      const res = await resources.uploadPluginVersion(file as File);
      messageApi.success(`上传成功,解析版本号:${res.version}`);
      onSuccess?.(res);
      actionRef.current?.reload();
    } catch (e) {
      const status = statusOf(e);
      if (status === 400) messageApi.error('未匹配到插件(或匹配到多个),请检查包名后重试');
      else if (status === 409) messageApi.error('该版本已存在');
      else if (status === 413) messageApi.error('文件超出大小限制');
      else messageApi.error('上传失败,请重试');
      onError?.(e as Error);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {contextHolder}
      <Card title="上传插件包">
        <Upload.Dragger
          name="file"
          accept=".tgz"
          multiple={false}
          // 接管上传逻辑(走资源层),并隐藏默认上传列表(成功/失败均以 message 反馈)。
          customRequest={customRequest}
          showUploadList={false}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽 .tgz 插件包到此区域上传</p>
          <p className="ant-upload-hint">
            上传后将解析包内 package.json,按包名匹配插件并记录版本号。
          </p>
        </Upload.Dragger>
      </Card>

      <div>
        <Typography.Title level={5} style={{ marginTop: 0 }}>
          已上传版本
        </Typography.Title>
        <ProTable<PluginVersionRow>
          actionRef={actionRef}
          rowKey="id"
          columns={columns}
          // 服务端分页:把 ProTable 的 current/pageSize 映射成后端 page/pageSize,
          // 其余 params(如 pluginId 过滤)平铺透传;读信封后返回 ProTable 约定结构。
          request={async (params) => {
            const { current, pageSize, ...filter } = params;
            const env = await resources.listPluginVersions<PluginVersionRow>({
              page: current,
              pageSize,
              ...filter,
            });
            return { data: env.rows, total: env.count, success: true };
          }}
          pagination={{ showSizeChanger: true }}
          // B2:开查询表单(按插件筛选),筛选值经上面 request 的 ...filter 透传给 listPluginVersions。
          search={{ labelWidth: 'auto', defaultCollapsed: false }}
          options={{ reload: true, density: false, setting: false }}
          dateFormatter="string"
        />
      </div>
    </Space>
  );
}
