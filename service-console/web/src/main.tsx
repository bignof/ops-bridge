import { lazy, StrictMode, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { createHashRouter, Navigate, RouterProvider } from 'react-router-dom';
import { App as AntdApp, ConfigProvider, Spin } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import { AuthProvider } from './auth/AuthContext';
import RequireAuth from './auth/RequireAuth';
import LoginPage from './auth/LoginPage';
import AppShell from './layout/AppShell';

// 页面统一懒加载分包(Task 1 评审:首屏 bundle 偏大,从本任务起页面 lazy() 拆分)。
const NamespacesPage = lazy(() => import('./pages/NamespacesPage'));
const ServicesPage = lazy(() => import('./pages/ServicesPage'));
const PluginsPage = lazy(() => import('./pages/PluginsPage'));
const ServicePluginsPage = lazy(() => import('./pages/ServicePluginsPage'));
const PluginUploadPage = lazy(() => import('./pages/PluginUploadPage'));
const ReleasesPage = lazy(() => import('./pages/ReleasesPage'));
const FetchRecordsPage = lazy(() => import('./pages/FetchRecordsPage'));
const InstancesPage = lazy(() => import('./pages/InstancesPage'));
const NodesPage = lazy(() => import('./pages/NodesPage'));
const NodeOperationsPage = lazy(() => import('./pages/NodeOperationsPage'));

// 懒加载页面统一的 Suspense fallback(居中 loading)。
const lazyPage = (node: React.ReactNode) => (
  <Suspense
    fallback={
      <div style={{ display: 'flex', justifyContent: 'center', padding: 64 }}>
        <Spin />
      </div>
    }
  >
    {node}
  </Suspense>
);

// 路由表:各资源页组件挂到 AppShell 的 children,统一走 lazy()。
// 后续 Task 4-6(插件上传 / 发布 / 获取记录)继续按同样方式补挂。
const router = createHashRouter([
  { path: '/login', element: <LoginPage /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <AppShell />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/namespaces" replace /> },
      { path: 'namespaces', element: lazyPage(<NamespacesPage />) },
      { path: 'services', element: lazyPage(<ServicesPage />) },
      { path: 'plugins', element: lazyPage(<PluginsPage />) },
      { path: 'service-plugins', element: lazyPage(<ServicePluginsPage />) },
      { path: 'plugin-upload', element: lazyPage(<PluginUploadPage />) },
      { path: 'releases', element: lazyPage(<ReleasesPage />) },
      { path: 'fetch-records', element: lazyPage(<FetchRecordsPage />) },
      { path: 'instances', element: lazyPage(<InstancesPage />) },
      { path: 'nodes', element: lazyPage(<NodesPage />) },
      { path: 'node-operations', element: lazyPage(<NodeOperationsPage />) },
    ],
  },
]);

// antd <App> 仅为 **React 组件内** 的 `App.useApp()`(拿到受 ConfigProvider 主题/locale 包裹的
// message/notification/modal 实例)提供上下文。
// 注意(Minor-5 更正):client.ts 拦截器用的是 **静态** `message.error`,它走 antd 全局 holder,
// **不读** AppContext —— 故全局兜底 toast 不受此 <App> 影响(既不继承其上下文,也不被它“消除告警”)。
// 此处保留 <App> 是为将来组件内用 App.useApp();静态 message 的主题继承如有需要,另需在 bootstrap
// 用 ConfigProvider 注册受包裹的 holder(本控制台为内网 admin,默认全局 holder 已够用,暂不做)。
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN}>
      <AntdApp>
        <AuthProvider>
          <RouterProvider router={router} />
        </AuthProvider>
      </AntdApp>
    </ConfigProvider>
  </StrictMode>,
);
