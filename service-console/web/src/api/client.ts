import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { message } from 'antd';

// service-console 控制台统一 API 客户端。
// - baseURL '/' :与 FastAPI 同源(单容器托管),业务侧用完整路径(如 '/api/namespaces'、'/auth/login')。
// - 请求拦截:注入 sessionStorage 里的 Bearer token(刷新保活)。
// - 响应拦截:
//     · 401            → 清 token + 跳登录(hash 路由)。
//     · 其余 4xx/5xx   → 全局兜底 toast(取后端 detail,否则通用文案),除非该请求显式
//                        opt-out(`suppressGlobalError`,见下)。这样删除/轮换/回滚/重新激活/
//                        发布/命名空间创建等写失败不再静默吞(运维误以为成功 = 高危)。
//
// 防双 toast:CrudTable 的 409「编码已存在」、PluginUploadPage 的 400/409/413、ReleasesPage
// publish 的 409 是页面本地专门精确提示;这些请求传 `{ suppressGlobalError: true }`,拦截器检测到
// 即不再兜底(交页面处理),避免一次失败弹两条。其余请求一律由拦截器统一兜底。
declare module 'axios' {
  export interface AxiosRequestConfig {
    /** 置 true 则本请求的失败不走全局兜底 toast(由调用页自行精确提示),仅 401 仍统一处理。 */
    suppressGlobalError?: boolean;
  }
}

const client = axios.create({ baseURL: '/' });

client.interceptors.request.use((c) => {
  const t = sessionStorage.getItem('platform_token');
  if (t) c.headers.Authorization = `Bearer ${t}`;
  return c;
});

// 从后端错误响应体中取人类可读信息(FastAPI 习惯放 `detail`);取不到则给通用兜底文案。
const messageOf = (e: AxiosError): string => {
  const detail = (e.response?.data as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === 'string' && detail.trim() !== '') return detail;
  return '操作失败,请稍后重试';
};

client.interceptors.response.use(
  (r) => r,
  (e: AxiosError) => {
    if (e.response?.status === 401) {
      sessionStorage.removeItem('platform_token');
      location.hash = '#/login';
      return Promise.reject(e);
    }
    // 非 401:除非该请求显式 opt-out(页面自处理),否则全局兜底 toast,杜绝写失败静默吞。
    const suppressed = (e.config as InternalAxiosRequestConfig | undefined)?.suppressGlobalError;
    if (!suppressed) message.error(messageOf(e));
    return Promise.reject(e);
  },
);

export default client;
