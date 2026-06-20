import axios from 'axios';

// service-platform 控制台统一 API 客户端。
// - baseURL '/' :与 FastAPI 同源(单容器托管),业务侧用完整路径(如 '/api/namespaces'、'/auth/login')。
// - 请求拦截:注入 sessionStorage 里的 Bearer token(刷新保活)。
// - 响应拦截:401 → 清 token + 跳登录(hash 路由)。
const client = axios.create({ baseURL: '/' });

client.interceptors.request.use((c) => {
  const t = sessionStorage.getItem('platform_token');
  if (t) c.headers.Authorization = `Bearer ${t}`;
  return c;
});

client.interceptors.response.use(
  (r) => r,
  (e) => {
    if (e.response?.status === 401) {
      sessionStorage.removeItem('platform_token');
      location.hash = '#/login';
    }
    return Promise.reject(e);
  },
);

export default client;
