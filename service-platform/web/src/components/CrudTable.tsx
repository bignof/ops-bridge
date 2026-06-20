import { useRef, useState } from 'react';
import {
  ProTable,
  DrawerForm,
  type ActionType,
  type ProColumns,
  type ProFormColumnsType,
  BetaSchemaForm,
} from '@ant-design/pro-components';
import { Button, Form, Popconfirm, Space, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import * as resources from '../api/resources';

/** 行记录最小约束:必须有 id(用于 update/remove/rowKey)。 */
export interface RowWithId {
  id: string | number;
}

export interface CrudTableProps<T extends RowWithId> {
  /** 资源名(对应 /api/<resource>),内部 list/create/update/remove 据此调用。 */
  resource: string;
  /** ProTable 列定义。 */
  columns: ProColumns<T>[];
  /** 新建 / 编辑 Drawer 的表单字段(BetaSchemaForm columns)。 */
  formFields: ProFormColumnsType<T>[];
  /** Drawer 标题前缀(如「命名空间」→「新建命名空间」/「编辑命名空间」)。 */
  title?: string;
  /** 工具条额外按钮(如自定义批量动作),渲染在「添加」左侧。 */
  toolBarExtra?: React.ReactNode;
  /** 行操作额外按钮渲染器(如命名空间的「轮换密钥」),拼在「编辑/删除」前。 */
  rowExtraActions?: (record: T) => React.ReactNode;
  /** 是否可编辑(关联表如 service-plugins 仅增删,无编辑);默认 true。 */
  editable?: boolean;
  /** 是否可删除;默认 true。 */
  deletable?: boolean;
  /**
   * A1 级联清空映射:`{ 父字段: [需清空的下级字段...] }`。父字段值变更时,自动把所有下级字段清空,
   * 避免「换上级后下级残留旧选值」造成错配提交(如 service-plugins 的 命名空间→服务→插件 三级级联)。
   */
  cascadeChildren?: Record<string, string[]>;
  /**
   * 提交前对表单值做变换(C3):如 service-plugins 仅用 namespaceId 做级联,提交时须裁掉它只发
   * `{serviceId, pluginId}`(后端 extra='ignore' 虽会静默丢弃,但前端裁干净更显式、不留隐患)。
   */
  transformValues?: (values: Record<string, unknown>) => Record<string, unknown>;
  /**
   * 创建成功回调:拿到 create 响应体(可能含 show-once 明文,如 { agentKey })。
   * 命名空间页据此在响应含 agentKey 时弹 ShowOnceModal。
   */
  onCreated?: (created: unknown) => void;
}

/**
 * 通用 CRUD 表格:ProTable 列表(服务端分页) + 工具条(添加/刷新,筛选走列 search)
 * + Drawer 表单(新建/编辑) + 行「编辑/删除」。内部统一调 `../api/resources`。
 *
 * 关键约束(对齐 P1a 契约):
 * - 服务端分页:ProTable `request` 把 antd 的 `current`/`pageSize` 映射为后端 `page`/`pageSize`,
 *   读统一信封 `{count, rows}` 后返回 `{ data: rows, total: count, success: true }`。
 * - 唯一冲突 409 → message.error('编码已存在');其它错误由 client 拦截器既有提示兜底。
 */
export default function CrudTable<T extends RowWithId>({
  resource,
  columns,
  formFields,
  title = '',
  toolBarExtra,
  rowExtraActions,
  editable = true,
  deletable = true,
  cascadeChildren,
  transformValues,
  onCreated,
}: CrudTableProps<T>) {
  const actionRef = useRef<ActionType>();
  const [messageApi, contextHolder] = message.useMessage();
  // 表单实例:A1 级联清空靠它在父级变更时 setFieldsValue(下级=undefined)。
  const [form] = Form.useForm();

  // Drawer 状态:open + 当前编辑记录(null = 新建)。
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<T | null>(null);

  const reload = () => actionRef.current?.reload();

  // A1 级联清空:父字段变更时清空其所有下级字段(防换上级后下级残留旧选值造成错配)。
  const handleValuesChange = (changed: Record<string, unknown>) => {
    if (!cascadeChildren) return;
    for (const parent of Object.keys(cascadeChildren)) {
      if (parent in changed) {
        const cleared: Record<string, undefined> = {};
        for (const child of cascadeChildren[parent]) cleared[child] = undefined;
        form.setFieldsValue(cleared);
      }
    }
  };

  // 统一错误处理:唯一冲突 409 给明确文案;其余给通用兜底(返回 false 让 DrawerForm 不关闭)。
  // 注:create/update 在资源层 opt-out 全局兜底(suppressGlobalError),故此处必须自管全部错误 UX,
  //     否则非 409 写失败会静默吞(A2)。401 仍由 client 拦截器统一处理(清 token + 跳登录)。
  const handleWriteError = (e: unknown): boolean => {
    const status =
      typeof e === 'object' && e && 'response' in e
        ? (e as { response?: { status?: number } }).response?.status
        : undefined;
    if (status === 409) messageApi.error('编码已存在');
    else if (status !== 401) messageApi.error('操作失败,请稍后重试');
    return false;
  };

  const openCreate = () => {
    setEditing(null);
    setDrawerOpen(true);
  };

  const openEdit = (record: T) => {
    setEditing(record);
    setDrawerOpen(true);
  };

  // Drawer 提交:editing 为空走 create,否则 update;成功后关抽屉、刷新列表。
  const handleSubmit = async (values: Record<string, unknown>): Promise<boolean> => {
    // C3:提交前按需裁剪/变换字段(如 service-plugins 裁掉仅用于级联的 namespaceId)。
    const payload = transformValues ? transformValues(values) : values;
    try {
      if (editing) {
        await resources.update(resource, editing.id, payload);
        messageApi.success('保存成功');
      } else {
        const created = await resources.create(resource, payload);
        messageApi.success('创建成功');
        onCreated?.(created);
      }
      reload();
      return true; // DrawerForm 返回 true 自动关闭
    } catch (e) {
      return handleWriteError(e);
    }
  };

  const handleDelete = async (record: T) => {
    try {
      await resources.remove(resource, record.id);
      messageApi.success('删除成功');
      reload();
    } catch {
      // 删除失败由 client 拦截器提示;此处不额外处理。
    }
  };

  // 操作列:额外动作(如轮换密钥) + 编辑 + 删除,按 props 开关拼装。
  const optionColumn: ProColumns<T> = {
    title: '操作',
    valueType: 'option',
    key: 'option',
    render: (_dom, record) => {
      const actions: React.ReactNode[] = [];
      if (rowExtraActions) actions.push(<span key="extra">{rowExtraActions(record)}</span>);
      if (editable)
        actions.push(
          <a key="edit" onClick={() => openEdit(record)}>
            编辑
          </a>,
        );
      if (deletable)
        actions.push(
          <Popconfirm
            key="delete"
            title="确认删除?"
            okText="删除"
            cancelText="取消"
            onConfirm={() => handleDelete(record)}
          >
            <a style={{ color: '#ff4d4f' }}>删除</a>
          </Popconfirm>,
        );
      return <Space size="middle">{actions}</Space>;
    },
  };

  const allColumns: ProColumns<T>[] =
    editable || deletable || rowExtraActions ? [...columns, optionColumn] : columns;

  return (
    <>
      {contextHolder}
      <ProTable<T>
        actionRef={actionRef}
        rowKey="id"
        columns={allColumns}
        // 服务端分页:把 ProTable 的 current/pageSize 映射成后端 page/pageSize,
        // 其余 params(列 search 产生的过滤)平铺透传;读信封后返回 ProTable 约定结构。
        request={async (params) => {
          const { current, pageSize, ...filter } = params;
          const env = await resources.list<T>(resource, {
            page: current,
            pageSize,
            ...filter,
          });
          return { data: env.rows, total: env.count, success: true };
        }}
        pagination={{ showSizeChanger: true }}
        search={false}
        options={{ reload: true, density: false, setting: false }}
        toolBarRender={() => [
          toolBarExtra,
          <Button key="add" type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            添加
          </Button>,
        ]}
        dateFormatter="string"
      />
      <DrawerForm<Record<string, unknown>>
        title={`${editing ? '编辑' : '新建'}${title}`}
        form={form}
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        // A1:父级字段变更时级联清空下级(按 cascadeChildren 配置),杜绝错配提交。
        onValuesChange={handleValuesChange}
        // 编辑时回填当前行;新建时清空。key 切换强制 DrawerForm 重置内部表单值。
        key={editing ? `edit-${editing.id}` : 'create'}
        initialValues={editing ?? {}}
        onFinish={handleSubmit}
        drawerProps={{ destroyOnClose: true }}
      >
        <BetaSchemaForm<Record<string, unknown>>
          layoutType="Embed"
          columns={formFields as ProFormColumnsType<Record<string, unknown>>[]}
        />
      </DrawerForm>
    </>
  );
}
