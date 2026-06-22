import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Drawer, Space, Tag, Typography } from 'antd';
import { ClearOutlined } from '@ant-design/icons';
import { parseSseFrames, chunkText } from './sseParser';

const { Text } = Typography;

// 实时 tail 行数(对齐原型 --tail;后端 AgentLogsStreamRequest.tail 约束 1..2000,200 为缺省值)。
const TAIL_LINES = 200;
// 终端保留行数上限:超出从顶部丢弃,避免长时间流式累积撑爆 DOM / 内存(对应 docker logs 滚动窗口)。
const MAX_LINES = 5000;
// 距底阈值(px):滚动条距底小于此值视为「贴底」,据此自动恢复跟随滚动。
const NEAR_BOTTOM_PX = 24;

export interface InstanceLogDrawerProps {
  open: boolean;
  /** 关闭回调(点关闭/遮罩/Esc 都走它);组件据此 abort 停流。 */
  onClose: () => void;
  /** 目标 agent(发现实例所属 agent);为空表示无可用目标(调用方应保证 open 时非空)。 */
  agentId: string | null;
  /** 目标目录(DiscoveredNode 的 compose 工程目录,发现权威);为空则不可拉日志。 */
  dir: string | null;
  /** 抽屉标题副标识(如容器名),便于用户辨认看的是哪个实例。 */
  title?: string;
}

/**
 * 实例实时日志查看器(抽屉)。接 console 既有 SSE:`POST /api/agents/{agentId}/logs/stream`。
 *
 * ⚠️ 为何不用 EventSource:该端点是 **POST + 需鉴权头**的 SSE,EventSource 只支持 GET、不能带头。
 * 故用 `fetch`(POST,带 `Authorization: Bearer`,JSON body)→ 读 `response.body` 的 ReadableStream
 * → `TextDecoder` 逐块解码 → 按 `\n\n` 解析 SSE 帧(取 `data:` 行)→ chunk 文本**追加**到终端。
 * `AbortController` 在关闭 / 卸载时 `abort()` 停流(后端断连即停该日志会话,不留孤儿)。
 *
 * 事件:`started`(流已建立)/ `chunk`(日志文本,追加)/ `finished`(流正常结束)/ `error`(显错误条)。
 * 鉴权:token 取法与 `api/client.ts` 一致(`sessionStorage['platform_token']`),baseURL 同源('/')。
 *   401/403 视为鉴权失败显式报错(不静默)——见 [[project_console_logs_sse_auth_conflict]]。
 *
 * 交互(贴原型日志查看器):等宽滚动日志区 + 自动滚到底(可关跟随);手动上滚自动取消跟随、贴底自动恢复;
 *   可清屏;状态条显示「实时 / 已结束 / 出错」+ 行数。
 */
export default function InstanceLogDrawer({ open, onClose, agentId, dir, title }: InstanceLogDrawerProps) {
  // 日志行(各为一条文本;chunk 内含多行时按 \n 拆开存,渲染更稳、行号/滚动更准)。
  const [lines, setLines] = useState<string[]>([]);
  // 流状态:idle(未开)/ connecting(请求中)/ streaming(收到 started)/ finished / error。
  const [status, setStatus] = useState<'idle' | 'connecting' | 'streaming' | 'finished' | 'error'>('idle');
  // 错误信息(error 事件 / 网络失败 / 鉴权失败时填,显在告警条)。
  const [errMsg, setErrMsg] = useState<string>('');
  // 自动滚动跟随(贴底时追加自动滚到底;用户上滚则关、贴底恢复)。
  const [follow, setFollow] = useState(true);

  // AbortController:关闭 / 卸载 / 重开时 abort 上一条流。用 ref 跨渲染持有。
  const abortRef = useRef<AbortController | null>(null);
  // 日志区 DOM:用于自动滚到底与判断是否贴底。
  const termRef = useRef<HTMLDivElement | null>(null);
  // follow 的最新值镜像:供流读取循环里(闭包)读到最新跟随态,避免闭包捕获旧值。
  const followRef = useRef(follow);
  followRef.current = follow;

  // 统一停止当前流(幂等):abort 并清空 controller 引用。
  const stopStream = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

  // 把文本切成行追加进终端(超上限从顶部丢弃);空 chunk 不产生行。
  const appendChunk = useCallback((text: string) => {
    if (text === '') return;
    // 末尾换行会产生一个空段,去掉避免凭空多一行;中间的空行保留(可能是日志本身的空行)。
    const incoming = text.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n');
    setLines((prev) => {
      const next = prev.concat(incoming);
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
    });
  }, []);

  // 打开抽屉即发起 SSE;关闭 / 卸载即 abort。依赖 open/agentId/dir:目标变了重连。
  useEffect(() => {
    if (!open) return;
    // 无目标(无 agentId/dir)不发起——调用方应已禁用入口,这里兜底防御。
    if (!agentId || !dir) {
      setStatus('error');
      setErrMsg('该实例缺少目录(dir)或所属 agent,无法拉取实时日志');
      return;
    }

    // 重置本次会话状态。
    setLines([]);
    setErrMsg('');
    setFollow(true);
    setStatus('connecting');

    const controller = new AbortController();
    abortRef.current = controller;

    // token 取法与 api/client.ts 一致(sessionStorage 的 Bearer);baseURL 同源 '/'。
    const token = sessionStorage.getItem('platform_token');

    const run = async () => {
      try {
        const resp = await fetch(`/api/agents/${encodeURIComponent(agentId)}/logs/stream`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({ dir, tail: TAIL_LINES, timestamps: true }),
          signal: controller.signal,
        });

        if (!resp.ok) {
          // 鉴权 / 业务失败:显式报错(不静默)。401/403 单独提示,其余给通用文案 + 状态码。
          if (resp.status === 401 || resp.status === 403) {
            setErrMsg(`鉴权失败(${resp.status}):无权查看该实例日志,请重新登录或确认权限`);
          } else if (resp.status === 404) {
            setErrMsg('未找到该 agent(可能已离线或未注册)');
          } else if (resp.status === 409) {
            setErrMsg('agent 离线或连接不可用,无法拉取实时日志');
          } else {
            setErrMsg(`日志流建立失败(HTTP ${resp.status})`);
          }
          setStatus('error');
          return;
        }
        if (!resp.body) {
          setErrMsg('当前环境不支持流式响应(response.body 为空)');
          setStatus('error');
          return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        // 逐块读取 → 解码 → 累积缓冲 → 解析出完整 SSE 帧 → 按事件处理。
        // 循环条件:reader.read() 直到 done;abort 时 read() 抛 AbortError 走 catch。
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const { events, rest } = parseSseFrames(buffer);
          buffer = rest;
          for (const evt of events) {
            if (evt.event === 'started') {
              setStatus('streaming');
            } else if (evt.event === 'chunk') {
              setStatus('streaming');
              appendChunk(chunkText(evt.data));
            } else if (evt.event === 'finished') {
              setStatus('finished');
            } else if (evt.event === 'error') {
              const detail = evt.data['message'] ?? evt.data['error'] ?? evt.data['detail'];
              setErrMsg(typeof detail === 'string' && detail !== '' ? detail : '日志流发生错误');
              setStatus('error');
            }
          }
        }
        // 流自然结束(后端关闭连接)而未收到 finished/error:标记结束(不当错误)。
        setStatus((s) => (s === 'error' ? s : 'finished'));
      } catch (e) {
        // abort 触发的 AbortError 是预期(关闭/卸载/重连),不报错。
        if (e instanceof DOMException && e.name === 'AbortError') return;
        if (controller.signal.aborted) return;
        setErrMsg(e instanceof Error && e.message ? e.message : '日志流连接失败');
        setStatus('error');
      }
    };

    void run();

    // 清理:关闭抽屉 / 卸载 / 依赖变更 → abort 当前流(后端断连即停会话)。
    return () => {
      controller.abort();
      if (abortRef.current === controller) abortRef.current = null;
    };
  }, [open, agentId, dir, appendChunk]);

  // 追加行后,若处于跟随态则滚到底(读 ref 取最新 follow,避免把 follow 列入依赖反复重订阅)。
  useEffect(() => {
    if (!followRef.current) return;
    const el = termRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines]);

  // 用户滚动:上滚(离底)→ 关跟随;手动滚回贴底 → 恢复跟随。
  const onTermScroll = () => {
    const el = termRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    if (atBottom !== followRef.current) setFollow(atBottom);
  };

  // 清屏(只清显示,不断流)。
  const clearLines = () => setLines([]);

  // 关闭:先停流(abort),再回调父级关抽屉。
  const handleClose = () => {
    stopStream();
    onClose();
  };

  // 状态条徽章文案。
  const statusTag = () => {
    if (status === 'streaming') return <Tag color="green">● 实时</Tag>;
    if (status === 'connecting') return <Tag color="blue">连接中…</Tag>;
    if (status === 'finished') return <Tag>日志流结束</Tag>;
    if (status === 'error') return <Tag color="red">出错</Tag>;
    return <Tag>未开始</Tag>;
  };

  return (
    <Drawer
      title={
        <Space size={8}>
          <span>实例日志</span>
          {title ? <Text type="secondary" style={{ fontSize: 13 }}>· {title}</Text> : null}
        </Space>
      }
      width={860}
      open={open}
      onClose={handleClose}
      destroyOnClose
      // 抽屉额外操作:清屏 + 关闭(关闭头部已有 × ,这里再放一个显式按钮,贴原型)。
      extra={
        <Space>
          <Button size="small" icon={<ClearOutlined />} onClick={clearLines}>
            清屏
          </Button>
          <Button size="small" onClick={handleClose}>
            关闭
          </Button>
        </Space>
      }
      styles={{ body: { display: 'flex', flexDirection: 'column', padding: 0 } }}
    >
      {/* 副说明:经 agent 实时 tail,仅实时不落库(贴原型文案)。 */}
      <div style={{ padding: '10px 16px', borderBottom: '1px solid #f0f0f0' }}>
        <Space wrap>
          <Text type="secondary" style={{ fontSize: 12.5 }}>
            经 agent 实时 tail(docker compose logs -f --tail {TAIL_LINES}),仅实时、不落库
          </Text>
          <Checkbox checked={follow} onChange={(e) => setFollow(e.target.checked)}>
            自动滚动
          </Checkbox>
        </Space>
      </div>

      {/* 错误条:鉴权 / 网络 / 后端 error 事件统一显式呈现(不静默)。 */}
      {status === 'error' && errMsg ? (
        <div style={{ padding: '10px 16px 0' }}>
          <Alert type="error" showIcon message={errMsg} />
        </div>
      ) : null}

      {/* 等宽终端区:可滚动,占满剩余高度;监听滚动以联动跟随态。 */}
      <div
        ref={termRef}
        onScroll={onTermScroll}
        data-testid="log-terminal"
        style={{
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          margin: '12px 16px',
          padding: 12,
          background: '#1e1e1e',
          color: '#d4d4d4',
          fontFamily:
            "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
          fontSize: 12.5,
          lineHeight: 1.6,
          borderRadius: 6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        {lines.length === 0 ? (
          <span style={{ color: '#888' }}>
            {status === 'connecting' ? '正在连接日志流…' : status === 'finished' ? '(无日志)' : ''}
          </span>
        ) : (
          lines.map((ln, i) => (
            // 流式日志行天然无稳定 id,index 作 key(只追加 + 顶部裁剪,不在中间增删,index 稳定可接受)。
            <div key={i}>{ln}</div>
          ))
        )}
      </div>

      {/* 状态条:实时/结束/出错 徽章 + 行数。 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '8px 16px',
          borderTop: '1px solid #f0f0f0',
        }}
      >
        {statusTag()}
        <span style={{ flex: 1 }} />
        <Text type="secondary" style={{ fontSize: 12 }}>
          {lines.length} 行 · 上限 {MAX_LINES} 行
        </Text>
      </div>
    </Drawer>
  );
}
