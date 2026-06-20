import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createHashRouter, Navigate, RouterProvider } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import { AuthProvider } from './auth/AuthContext';
import RequireAuth from './auth/RequireAuth';
import LoginPage from './auth/LoginPage';
import AppShell from './layout/AppShell';
import NamespacesPlaceholder from './pages/NamespacesPlaceholder';

// 路由表:后续 Task 2-6 会把各资源页组件挂到 AppShell 的 children。
// 本任务仅命名空间一个占位,其余菜单项路由在后续任务补齐。
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
      { path: 'namespaces', element: <NamespacesPlaceholder /> },
    ],
  },
]);

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </ConfigProvider>
  </StrictMode>,
);
