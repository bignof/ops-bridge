import { useMemo } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Avatar, Dropdown, Layout, Menu, Select, Space, Typography, type MenuProps } from 'antd';
import {
  ApiOutlined,
  AppstoreOutlined,
  AuditOutlined,
  ClusterOutlined,
  CloudUploadOutlined,
  DeploymentUnitOutlined,
  DesktopOutlined,
  HistoryOutlined,
  LogoutOutlined,
  PartitionOutlined,
  PictureOutlined,
  RocketOutlined,
  SendOutlined,
  SyncOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useAuth } from '../auth/AuthContext';
import { useNamespace } from '../context/NamespaceContext';

// 命名空间切换器「全部命名空间」哨兵值(空串):antd Select value 必须是基元,用空串代表「不过滤」。
// 与 sessionStorage 持久化的 null 语义对应(见 NamespaceContext)。
const ALL_NS_VALUE = '';

const { Header, Sider, Content } = Layout;

// 左侧栏三组菜单:
//   配置:命名空间 / 服务 / 插件 / 服务配置(选服务 → 绑定/解绑插件 + 改版本)/ 镜像配置(选服务 → 镜像台账 + 设为当前)
//   发布:插件上传 / 插件发布 / 获取记录
//   运维:服务对账(意图⋈现实,纳管收件箱) / 投放记录(投放运行记录 + 逐实例进度 + 失败重试/回滚) / 实例(发现只读) / 节点(启停重启重部署 + 二次确认) / 操作审计(只读命令审计)
// key 即对应路由 path(供 createHashRouter 的 children 挂载)。
const MENU_ITEMS: MenuProps['items'] = [
  {
    key: 'grp-config',
    label: '配置',
    type: 'group',
    children: [
      { key: '/namespaces', icon: <PartitionOutlined />, label: '命名空间' },
      { key: '/services', icon: <DeploymentUnitOutlined />, label: '服务' },
      { key: '/plugins', icon: <AppstoreOutlined />, label: '插件' },
      { key: '/service-plugins', icon: <ApiOutlined />, label: '服务配置' },
      { key: '/service-images', icon: <PictureOutlined />, label: '镜像配置' },
    ],
  },
  {
    key: 'grp-release',
    label: '发布',
    type: 'group',
    children: [
      { key: '/plugin-upload', icon: <CloudUploadOutlined />, label: '插件上传' },
      { key: '/releases', icon: <RocketOutlined />, label: '插件发布' },
      { key: '/fetch-records', icon: <HistoryOutlined />, label: '获取记录' },
    ],
  },
  {
    key: 'grp-ops',
    label: '运维',
    type: 'group',
    children: [
      { key: '/reconciliation', icon: <SyncOutlined />, label: '服务对账' },
      { key: '/rollouts', icon: <SendOutlined />, label: '投放记录' },
      { key: '/instances', icon: <DesktopOutlined />, label: '实例' },
      { key: '/nodes', icon: <ClusterOutlined />, label: '节点' },
      { key: '/node-operations', icon: <AuditOutlined />, label: '操作审计' },
    ],
  },
];

export default function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useAuth();
  const { namespace, setNamespace, options, optionsLoading } = useNamespace();

  // 根路径('/')默认高亮命名空间。
  const selectedKey = location.pathname === '/' ? '/namespaces' : location.pathname;

  const userMenu: MenuProps['items'] = useMemo(
    () => [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
    [],
  );

  const onUserMenuClick: MenuProps['onClick'] = (info) => {
    if (info.key === 'logout') logout();
  };

  // 命名空间切换器 options:置顶「全部命名空间」(哨兵空串)+ 各命名空间(label=code,value=id 串)。
  const nsSelectOptions = useMemo(
    () => [
      { label: '全部命名空间', value: ALL_NS_VALUE },
      ...options.map((n) => ({ label: n.code || String(n.id), value: String(n.id) })),
    ],
    [options],
  );

  // 切换器当前值:选了具体命名空间显其 id 串,否则「全部」哨兵。
  const nsValue = namespace ? String(namespace.id) : ALL_NS_VALUE;

  // 选中变化:空串 → 切回「全部」(setNamespace(null));否则按 id 反查命名空间行,带上 code 一并存。
  const onNamespaceChange = (value: string) => {
    if (value === ALL_NS_VALUE) {
      setNamespace(null);
      return;
    }
    const row = options.find((n) => String(n.id) === value);
    // 理论上选项均来自 options,必能命中;命不中(脏选项)则保守切回全部,避免存半截脏值。
    setNamespace(row ? { id: row.id, code: row.code } : null);
  };

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider theme="light" width={220} style={{ borderRight: '1px solid #f0f0f0' }}>
        <div
          style={{
            height: 56,
            display: 'flex',
            alignItems: 'center',
            padding: '0 20px',
            fontWeight: 600,
            fontSize: 16,
          }}
        >
          服务插件分发平台
        </div>
        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          items={MENU_ITEMS}
          onClick={(info) => navigate(info.key)}
          style={{ borderInlineEnd: 'none' }}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: '#fff',
            borderBottom: '1px solid #f0f0f0',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            paddingInline: 24,
          }}
        >
          {/* 左侧:命名空间切换器(总览↔下钻闭环的总闸)。选「全部命名空间」= 跨 ns 全集 + 服务聚合;
              选具体 ns = 各页只剩该 ns 数据。选中持久化(sessionStorage),刷新保活。 */}
          <Space size={8}>
            <Typography.Text type="secondary">命名空间:</Typography.Text>
            <Select<string>
              // aria-label 给无障碍/测试稳定定位(antd Select 自身无 label 关联)。
              aria-label="命名空间切换器"
              showSearch
              optionFilterProp="label"
              style={{ minWidth: 220 }}
              loading={optionsLoading}
              value={nsValue}
              options={nsSelectOptions}
              onChange={onNamespaceChange}
            />
          </Space>

          <Dropdown menu={{ items: userMenu, onClick: onUserMenuClick }} placement="bottomRight">
            <span style={{ cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <Avatar size="small" icon={<UserOutlined />} />
              <Typography.Text>{user ?? '管理员'}</Typography.Text>
            </span>
          </Dropdown>
        </Header>
        <Content style={{ margin: 16, padding: 16, background: '#fff', borderRadius: 8 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
