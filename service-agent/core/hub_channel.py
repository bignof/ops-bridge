"""
hub_channel.py — 与交付中枢「一问一答」的传输层：ops_code（运维口令校验）和 hub_rpc（环境开关、实例快照）
共用，各持一个 HubChannel 实例。并发最敏感的这一块只在这里维护一份。

通道负责：
- 连接代次：连上、断开都让代次加一，并立即唤醒全部在途请求（结果记为 DISCONNECTED）；迟到的旧代次结果丢弃。
- 特性：每条新连接先置为「未知」（None），收到 hub_hello 后记下 features，断线清空；想等协商的请求在条件变量上等。
- 在途表：登记即占一个名额；结果和断线同时发生时先到的说了算，已经收到结果的请求不会被随后的断线改写。
- 发送：一个专用发送线程配有界队列。半开连接下 send 阻塞时只卡住这一个线程；旧连接的帧、调用方已放弃的帧
  直接丢弃，配置了 stale_seconds 的实例再丢排队过久的帧；发送失败按断连处理。

使用方（ops_code、hub_rpc）负责，通道不管：帧名和字段、要求中枢声明的特性名、协商期间怎么办
（ops_code 立即回 503 / 501，hub_rpc 等 hub_hello）、在途上限多大、调用方自己的超时、状态码和响应体的映射。

约定：register、wait_features、enqueue 以及读写 connected / features / connected_at / generation / pending，
都要求调用方持有 lock（wait_features 等待期间条件变量会放开它）；其余方法自己加锁。
日志不记帧的内容：发送异常的信息里可能带着整帧，只记帧类型和异常类型。
"""
import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)

DISCONNECTED = object()   # 在途条目的结果：连接断了（或换了），没等到中枢的回复


def _settle(entry, result):
    """调用时持有 lock：给在途条目定下结果并唤醒等待者。已有结果的不改写（先到的说了算）。"""
    if not entry['event'].is_set():
        entry['result'] = result
        entry['event'].set()


class HubChannel:
    def __init__(self, name, queue_size, stale_seconds=None, clock=time.monotonic):
        """name 用在发送线程名上；queue_size 是发送队列容量；stale_seconds 不为 None 时，排队超过这么久的帧不再发送；
        clock 返回单调时钟的读数：使用方传「延迟查找自己模块里 _monotonic」的闭包，测试才换得动时钟。"""
        self.name = name
        self.stale_seconds = stale_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)   # 等协商的请求在这里睡；hub_hello 到、连接换代时叫醒
        self.generation = 0
        self.connected_at = None      # 当前连接建立时刻（clock）；None = 未连接
        self.features = None          # None = 当前连接还没收到 hub_hello
        self.sender = None
        self.pending = {}             # requestId -> {'event', 'generation', 'result'}，使用方可再加字段
        self.queue = queue.Queue(maxsize=queue_size)
        self._thread = None

    @property
    def connected(self):
        """调用时持有 lock：当前有一条已登记的连接。"""
        return self.sender is not None and self.connected_at is not None

    # ---- 连接生命周期：使用方模块把同名函数直接指到这里，ws_client 在 _on_open / _on_hub_hello / _on_close 里调用 ----

    def on_connected(self, sender):
        """新连接建立：换代，特性置为未知，换发送函数。
        断线时旧请求已唤醒过；这里再唤醒一遍是兜底，免得没走到 on_disconnected 的旧请求干等到超时。"""
        with self.lock:
            self._wake_all()
            self.generation += 1
            self.connected_at = self.clock()
            self.features = None
            self.sender = sender

    def set_features(self, features):
        """收到 hub_hello：记下本连接的特性并叫醒等协商的请求；features 不是列表时按「没有任何特性」处理。断线后迟到的调用忽略。"""
        with self.lock:
            if self.connected_at is None:
                return
            names = features if isinstance(features, list) else []
            self.features = frozenset(name for name in names if isinstance(name, str))
            self.cond.notify_all()

    def on_disconnected(self):
        """连接断开：换代、清特性，立即唤醒全部在途请求（返回 DISCONNECTED）。"""
        with self.lock:
            self.generation += 1
            self.connected_at = None
            self.features = None
            self.sender = None
            self._wake_all()

    def resolve(self, data):
        """收到结果帧：按 requestId 交给对应的在途请求，条目的结果是去掉 requestId 后的整帧（type 还在，由使用方取舍）。
        未知、已结束（超时放弃、已有结果或已断线）、旧连接代次的结果丢弃。返回是否交到了。"""
        request_id = data.get('requestId')
        with self.lock:
            entry = self.pending.get(request_id) if isinstance(request_id, str) else None
            if entry is None or entry['generation'] != self.generation or entry['event'].is_set():
                return False
            _settle(entry, {key: value for key, value in data.items() if key != 'requestId'})
            return True

    def _wake_all(self):
        """调用时持有 lock：让还没有结果的在途请求返回 DISCONNECTED，并叫醒在等协商的请求。"""
        for entry in self.pending.values():
            _settle(entry, DISCONNECTED)
        self.cond.notify_all()

    # ---- 在途请求 ----

    def register(self, request_id, limit):
        """调用时持有 lock：登记一个属于当前连接的在途请求（占一个名额）；在途已有 limit 个返回 None。"""
        if len(self.pending) >= limit:
            return None
        entry = {'event': threading.Event(), 'generation': self.generation, 'result': None}
        self.pending[request_id] = entry
        return entry

    def wait_features(self, entry, until):
        """调用时持有 lock（等待期间放开）：等到特性已知、条目已有结果（断线或换了连接）或时钟走到 until。
        返回后由调用方自己看状态：特性仍是 None 且条目没结果，就是时间到了。"""
        self.cond.wait_for(lambda: self.features is not None or entry['event'].is_set(),
                           max(0.0, until - self.clock()))

    def enqueue(self, request_id, entry, frame):
        """调用时持有 lock：把帧放进发送队列并确保发送线程在跑。队列满时撤掉登记并返回 False。"""
        try:
            self.queue.put_nowait((self.clock(), entry['generation'], request_id, frame))
        except queue.Full:
            self.pending.pop(request_id, None)
            return False
        self._ensure_thread()
        return True

    def release(self, request_id):
        """调用方拿到结果或放弃（超时）后撤掉登记，在途表因此始终有界。"""
        with self.lock:
            self.pending.pop(request_id, None)

    # ---- 发送线程 ----

    def send_one(self, item):
        """发送线程处理一帧：旧连接的、调用方已放弃的、（配置了的话）排队过久的帧直接丢弃。返回是否已发出。"""
        enqueued_at, generation, request_id, frame = item
        with self.lock:
            sender = self.sender if generation == self.generation and request_id in self.pending else None
        if sender is None or (self.stale_seconds is not None and self.clock() - enqueued_at > self.stale_seconds):
            return False
        try:
            sender(frame)
            return True
        except Exception as exc:  # noqa: BLE001 —— 发送失败按断连处理；异常信息可能含整帧，只记类型
            logger.warning('%s 发送失败（%s），按断连处理', frame.get('type'), type(exc).__name__)
            self._fail(request_id, generation)
            return False

    def _fail(self, request_id, generation):
        with self.lock:
            entry = self.pending.get(request_id)
            if entry is not None and entry['generation'] == generation:
                _settle(entry, DISCONNECTED)

    def _send_loop(self):
        while True:
            self.send_one(self.queue.get())

    def _ensure_thread(self):
        """调用时持有 lock：发送线程不存在或已退出时启动一个，全程至多一个。"""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._send_loop, name=f'{self.name}-sender', daemon=True)
            self._thread.start()

    def reset(self):
        """测试用：清空连接、特性、在途请求和发送队列。发送线程保留，队列空了它就阻塞在 get 上。"""
        with self.lock:
            self._wake_all()
            self.pending.clear()
            self.generation += 1
            self.connected_at = None
            self.features = None
            self.sender = None
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
