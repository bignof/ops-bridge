import { useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Button, Card, Form, Input, Typography, message } from 'antd';
import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { useAuth } from './AuthContext';

interface LoginForm {
  username: string;
  password: string;
}

/** 登录页:账号密码 → POST /auth/login。成功后跳转登录前的目标(默认 '/')。 */
export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [loading, setLoading] = useState(false);
  const [messageApi, contextHolder] = message.useMessage();

  // RequireAuth 跳转登录时把原目标塞在 state.from;登录成功后跳回去。
  const from = (location.state as { from?: string } | null)?.from ?? '/';

  const onFinish = async (values: LoginForm) => {
    setLoading(true);
    try {
      await login(values.username, values.password);
      navigate(from, { replace: true });
    } catch (e: unknown) {
      const status =
        typeof e === 'object' && e && 'response' in e
          ? (e as { response?: { status?: number } }).response?.status
          : undefined;
      messageApi.error(status === 401 ? '用户名或密码错误' : '登录失败,请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f0f2f5',
      }}
    >
      {contextHolder}
      <Card style={{ width: 380, boxShadow: '0 2px 8px rgba(0,0,0,0.08)' }}>
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 24 }}>
          服务插件分发平台
        </Typography.Title>
        <Form<LoginForm> name="login" layout="vertical" onFinish={onFinish} autoComplete="off">
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: '请输入用户名' }]}
          >
            <Input prefix={<UserOutlined />} placeholder="请输入用户名" autoFocus />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="请输入密码" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" block loading={loading}>
              登录
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  );
}
