"""日志文件协议（logfile_*）与 compose 发现的常量。刻意不做 env：改值 = 改协议，须与 hub 侧 spec §3.2.6 同步。"""

# 实时跟随
FOLLOW_POLL_SEC = 0.25          # 轮询文件增长的间隔
RELIST_SEC = 5.0                # 重新扫描子目录找更新文件的间隔
FLUSH_SEC = 0.2                 # 待发条目最长攒批时间
FLUSH_ENTRIES = 200             # 攒到多少条立即刷一帧
FLUSH_BYTES = 64 * 1024         # 攒到多少字节立即刷一帧
RATE_ENTRIES_PER_SEC = 2000     # 令牌桶稳态速率（条/秒）
RATE_BURST = 4000               # 令牌桶容量
MAX_FOLLOW_SESSIONS = 8         # 同时最多几条跟随会话（与中枢 LOG_STREAM_PER_AGENT 同值，容纳单机多实例）
TAIL_DEFAULT = 200              # 起手回放条数缺省
TAIL_MAX = 500                  # 起手回放条数上限
TAIL_LOOKBACK_BYTES = 8 * 1024 * 1024  # 起手回放最多向前读多少字节
FOLLOW_READ_MAX_BYTES = 1024 * 1024    # 单轮轮询最多读入多少增量字节（剩余留到下一轮）
FOLLOW_LINE_MAX_BYTES = 1024 * 1024    # 未闭合行的缓冲上限，超限强制断行发出
FOLLOW_READ_MAX_BYTES = 1024 * 1024    # 单轮轮询最多读入多少增量字节（剩余留到下一轮）
FOLLOW_LINE_MAX_BYTES = 1024 * 1024    # 未闭合行的缓冲上限，超限强制断行发出

# 拉取上传
MAX_FETCH_CONCURRENCY = 1
FETCH_CHUNK_BYTES = 256 * 1024
FETCH_GZIP_LEVEL = 6
FETCH_CONNECT_TIMEOUT_SEC = 10
FETCH_READ_TIMEOUT_SEC = 600

# 列文件
LIST_MAX_DEPTH = 2
LIST_MAX_FILES = 500

# compose 发现
DISCOVER_MAX_DEPTH = 3
DISCOVER_MAX_PROJECTS = 200
