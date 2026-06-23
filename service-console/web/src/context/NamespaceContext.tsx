import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import * as resources from '../api/resources';
import type { NamespaceRow } from '../api/resources';

// 顶栏命名空间切换器(P3-10)的全局选中态 + 可选项,供所有页面下钻过滤共享。
// 设计要点:
//  - 选中态同时持有 `id`(数值)与 `code`(== 发现侧 agentId)。下游两套过滤口径各取所需:
//      · services / fetch-records 等台账页 → 用 `id` 走 ?namespaceId=。
//      · instances 发现页           → 用 `code` 走 ?namespace=(agentId)。
//    (口径来源见 resources.ts NamespaceRow 注释 / 对账页「发现侧 agentId 即命名空间 code」。)
//  - `null` 语义 = 「全部命名空间」(不传任何过滤,后端返回全集),为默认值。
//  - 选中态持久化到 sessionStorage(键 platform_ns),刷新保活(与 auth token 同机制,登出/关页即清)。

const NS_KEY = 'platform_ns';

/** 选中的某个命名空间(id 给台账页过滤、code 给发现页过滤);null = 全部命名空间。 */
export interface SelectedNamespace {
  id: string | number;
  code: string;
}

export interface NamespaceContextValue {
  /** 当前选中命名空间;null = 「全部命名空间」(不过滤)。 */
  namespace: SelectedNamespace | null;
  /** 切换当前命名空间(null = 切回全部);写入 sessionStorage 持久化。 */
  setNamespace: (ns: SelectedNamespace | null) => void;
  /** 命名空间可选项(切换器渲染用);首屏拉一次,失败为空数组(切换器仍可用「全部」)。 */
  options: NamespaceRow[];
  /** 选项是否加载中(切换器据此显 loading)。 */
  optionsLoading: boolean;
}

// 导出原始 Context 供测试构造受控 Provider(喂定 namespace 值,免触发真实 listNamespaces 拉取);
// 业务代码仍只用 NamespaceProvider / useNamespace。
export const NamespaceContext = createContext<NamespaceContextValue | null>(null);

// 从 sessionStorage 恢复选中态(JSON 解析失败 / 字段不全 → 回落「全部」,不让脏值卡死切换器)。
const readPersisted = (): SelectedNamespace | null => {
  const raw = sessionStorage.getItem(NS_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<SelectedNamespace> | null;
    if (parsed && parsed.id !== undefined && parsed.id !== null && typeof parsed.code === 'string') {
      return { id: parsed.id, code: parsed.code };
    }
  } catch {
    /* 脏值忽略,回落全部 */
  }
  return null;
};

// 切换器选项 pageSize:取后端硬上限 200(各 list 端点 Query le=200,前后端必须一致;>200 需远程搜索)。
const NS_OPTIONS_PAGE_SIZE = 200;

export function NamespaceProvider({ children }: { children: ReactNode }) {
  const [namespace, setNamespaceState] = useState<SelectedNamespace | null>(() => readPersisted());
  const [options, setOptions] = useState<NamespaceRow[]>([]);
  const [optionsLoading, setOptionsLoading] = useState(true);

  // 首屏拉命名空间选项(切换器用)。失败静默置空(全局兜底 toast 已由 client 拦截器统一处理;
  // 切换器仍可工作 ——「全部」恒可选)。401 由拦截器统一跳登录。
  useEffect(() => {
    let alive = true;
    setOptionsLoading(true);
    resources
      .listNamespaces({ pageSize: NS_OPTIONS_PAGE_SIZE })
      .then((env) => {
        if (alive) setOptions(env.rows);
      })
      .catch(() => {
        if (alive) setOptions([]);
      })
      .finally(() => {
        if (alive) setOptionsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 切换 + 持久化:null 清除存储(回落全部),否则写入 {id, code}。
  const setNamespace = useCallback((ns: SelectedNamespace | null) => {
    setNamespaceState(ns);
    if (ns) sessionStorage.setItem(NS_KEY, JSON.stringify(ns));
    else sessionStorage.removeItem(NS_KEY);
  }, []);

  const value = useMemo<NamespaceContextValue>(
    () => ({ namespace, setNamespace, options, optionsLoading }),
    [namespace, setNamespace, options, optionsLoading],
  );

  return <NamespaceContext.Provider value={value}>{children}</NamespaceContext.Provider>;
}

export function useNamespace(): NamespaceContextValue {
  const ctx = useContext(NamespaceContext);
  if (!ctx) throw new Error('useNamespace 必须在 <NamespaceProvider> 内使用');
  return ctx;
}
