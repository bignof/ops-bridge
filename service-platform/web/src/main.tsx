import { lazy, StrictMode, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { createHashRouter, Navigate, RouterProvider } from 'react-router-dom';
import { ConfigProvider, Spin } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import { AuthProvider } from './auth/AuthContext';
import RequireAuth from './auth/RequireAuth';
import LoginPage from './auth/LoginPage';
import AppShell from './layout/AppShell';

// 页面统一懒加载分包(Task 1 评审:首屏 bundle 偏大,从本任务起页面 lazy() 拆分)。
const NamespacesPage = lazy(() => import('./pages/NamespacesPage'));

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

// 路由表:后续 Task 3-6 会把各资源页组件挂到 AppShell 的 children(同样走 lazy())。
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
