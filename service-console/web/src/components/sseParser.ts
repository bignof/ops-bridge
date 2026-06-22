// SSE(Server-Sent Events)帧增量解析工具。
// 抽成独立模块(非组件文件):既满足 react-refresh「组件文件只导出组件」约束,也便于单测纯函数。
// 后端帧格式见 `app/hub/routers/logs.py` `_encode_sse`:`event: <name>\ndata: <json>\n\n`。

/**
 * 一帧 SSE 事件:仅保留 `event` 名与 `data`(JSON 解析后的对象)。
 * data 里含后端透传字段(chunk 事件含日志文本;started/finished/error 含各自 body)。
 */
export interface SseEvent {
  event: string;
  data: Record<string, unknown>;
}

/**
 * 把流式累积缓冲解析成完整 SSE 帧序列(增量):按空行(`\n\n`)切分,
 * 返回 `{ events, rest }`——rest 是尚未收齐的残帧,留到下次拼接再解析。
 * 仅取 `event:` / `data:` 行(其余如 `id:`/`retry:`/注释忽略);`data:` 按 JSON 解析(失败兜底当纯文本)。
 */
export function parseSseFrames(buffer: string): { events: SseEvent[]; rest: string } {
  // SSE 规范帧分隔是空行;兼容 \r\n。先统一换行再按 \n\n 切。
  const normalized = buffer.replace(/\r\n/g, '\n');
  const parts = normalized.split('\n\n');
  // 最后一段可能是未收齐的残帧,留回缓冲(若原串以 \n\n 结尾,末段为空串,rest='' 正确)。
  const rest = parts.pop() ?? '';
  const events: SseEvent[] = [];
  for (const block of parts) {
    if (block.trim() === '') continue;
    let event = 'message';
    const dataLines: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice('event:'.length).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice('data:'.length).replace(/^ /, ''));
    }
    const raw = dataLines.join('\n');
    let data: Record<string, unknown> = {};
    if (raw !== '') {
      try {
        const parsed = JSON.parse(raw);
        data = parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : { text: raw };
      } catch {
        // 非 JSON(理论不会发生,后端固定 JSON);兜底当纯文本,不丢内容。
        data = { text: raw };
      }
    }
    events.push({ event, data });
  }
  return { events, rest };
}

/**
 * 从 chunk 事件 data 里取日志文本:后端把日志放在某字段(优先 `chunk`,兼容 `line`/`text`/`message`/`log`)。
 * 取不到返回空串(调用方据此不渲染空行)。
 */
export function chunkText(data: Record<string, unknown>): string {
  for (const key of ['chunk', 'line', 'text', 'message', 'log']) {
    const v = data[key];
    if (typeof v === 'string' && v !== '') return v;
  }
  return '';
}
