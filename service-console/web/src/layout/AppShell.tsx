import { useMemo } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Avatar, Dropdown, Layout, Menu, Typography, type MenuProps } from 'antd';
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
  RocketOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useAuth } from '../auth/AuthContext';

const { Header, Sider, Content } = Layout;

// 左侧栏三组菜单:
//   配置:命名空间 / 服务 / 插件 / 服务插件
//   发布:插件上传 / 插件发布 / 获取记录
//   运维:节点(启停重启重部署 + 二次确认) / 操作审计(只读命令审计)
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
      { key: '/service-plugins', icon: <ApiOutlined />, label: '服务插件' },
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

  // 根路径('/')默认高亮命名空间。
  const selectedKey = location.pathname === '/' ? '/namespaces' : location.pathname;

  const userMenu: MenuProps['items'] = useMemo(
    () => [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录' }],
    [],
  );

  const onUserMenuClick: MenuProps['onClick'] = (info) => {
    if (info.key === 'logout') logout();
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
            justifyContent: 'flex-end',
            paddingInline: 24,
          }}
        >
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
