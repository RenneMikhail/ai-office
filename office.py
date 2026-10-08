#!/usr/bin/env python3
"""AI Office 3D: Claude, Qwen и DeepSeek — три офисных сотрудника.

Запуск:  python3 office.py            (реальные модели)
         python3 office.py --demo     (без моделей, имитация)
Потом откройте http://localhost:8765  (откроется само)

Сервер — только стандартная библиотека. 3D-сцена в браузере использует
three.js, который подгружается с CDN (нужен интернет).
Остановка сервера (Ctrl+C или кнопка «Выключить офис») — сотрудники встают
и уходят из комнаты.
"""
import glob, json, os, secrets, queue, subprocess, sys, threading, time, urllib.request, webbrowser, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
# Онлайн-доступ: смотреть можно по ссылке с ключом, управлять (задачи, выключение) — только с этого компьютера.
TOKEN = os.environ.get("OFFICE_TOKEN") or secrets.token_urlsafe(12)
DEMO = "--demo" in sys.argv

# ---- Настройки сотрудников ---------------------------------------------------
# Чтобы экономить токены Claude, ТЗ и итог пишет Qwen (локально, бесплатно),
# а Claude только выполняет работу. Можно ещё удешевить: "model": "haiku".
AGENTS = {
    "claude_app": {  # приложение-чат Claude: в конвейере не участвует, только присутствует
        "name": "Claude", "role": "Чат-приложение", "kind": "none",
    },
    "claude": {  # Claude Code (его же запускает `claude -p` в конвейере)
        "name": "Claude Code", "role": "Исполнитель", "kind": "claude_cli",
        "model": None,
    },
    "qwen": {
        "name": "Qwen", "role": "Менеджер", "kind": "openai",
        "url": "http://localhost:1234/v1/chat/completions",  # LM Studio
        "model": "qwen/qwen3.5-9b", "key": "lm-studio",
        "extra": {"reasoning_effort": "none"},  # без долгих «размышлений»
    },
    "deepseek": {
        "name": "DeepSeek", "role": "Ревьюер", "kind": "openai",
        # Если есть DEEPSEEK_API_KEY — используется API DeepSeek.
        # Иначе поменяйте url/model на локальный сервер (LM Studio/Ollama).
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat", "key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "extra": {},
    },
}

# Конвейер: кто что делает и кому передаёт
PIPELINE = [
    ("qwen", "Ты менеджер. Превращаешь задачу пользователя в короткое чёткое ТЗ "
             "для исполнителя (до 100 слов).\n\nЗадача: {task}"),
    ("claude", "Ты исполнитель. Выполни работу по ТЗ. Ответ — только результат, "
               "кратко.\n\nТЗ: {prev}"),
    ("deepseek", "Ты ревьюер. Проверь работу, исправь ошибки и выдай улучшенную "
                 "версию. Кратко.\n\nРабота: {prev}"),
    ("qwen", "Ты менеджер. Оформи итоговый ответ пользователю по результату ревью. "
             "Кратко.\n\nИсходная задача: {task}\n\nРезультат ревью: {prev}"),
]

# ---- Вызов моделей -------------------------------------------------------------
def clean(t):
    return re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()

def ask(agent_id, prompt):
    a = AGENTS[agent_id]
    if DEMO:
        time.sleep(2.5)
        return f"[{a['name']}] демо-ответ на: {prompt[-60:].strip()}"
    if a["kind"] == "claude_cli":
        cmd = ["claude", "-p", prompt] + (["--model", a["model"]] if a.get("model") else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd="/tmp")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "claude вернул ошибку")
        return r.stdout.strip()
    if not a["key"]:
        raise RuntimeError("нет ключа (DEEPSEEK_API_KEY)")
    body = json.dumps({"model": a["model"], "max_tokens": 1000, **a.get("extra", {}),
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(a["url"], body, {
        "Content-Type": "application/json", "Authorization": "Bearer " + a["key"]})
    with urllib.request.urlopen(req, timeout=300) as resp:
        out = clean(json.load(resp)["choices"][0]["message"]["content"] or "")
    if not out:
        raise RuntimeError("пустой ответ модели")
    return out

# ---- События для браузера (SSE) -----------------------------------------------
listeners, lock, busy = [], threading.Lock(), threading.Event()

event_log, event_cond = [], threading.Condition()  # для зрителей через туннель (SSE там не работает)
event_seq = [0]

def emit(**ev):
    with lock:
        for q in listeners:
            q.put(ev)
    with event_cond:
        event_seq[0] += 1
        event_log.append((event_seq[0], ev))
        del event_log[:-300]
        event_cond.notify_all()

def snapshot():
    return ([{"type": "activity", "agent": k, "active": v} for k, v in activity.items()]
            + [{"type": "detail", "agent": k, "text": v} for k, v in details.items()]
            + [{"type": "presence", "agent": k, "here": bool(t), "token": t or ""} for k, t in presence.items()])

def poll(since):
    """Long polling: отдать события новее since (или снимок состояния при since < 0)."""
    with event_cond:
        if since < 0:
            return {"id": event_seq[0], "events": snapshot()}
        if event_seq[0] <= since:
            event_cond.wait(timeout=8)
        return {"id": event_seq[0], "events": [e for i, e in event_log if i > since]}

def run_task(task):
    try:
        prev, last = "", None
        for agent, tpl in PIPELINE:
            if last and last != agent:
                emit(type="handoff", src=last, dst=agent, text=prev)
            emit(type="working", agent=agent)
            try:
                prev = ask(agent, tpl.format(task=task, prev=prev))
            except Exception as e:
                emit(type="error", agent=agent, text=str(e)[:200])
                return
            emit(type="done", agent=agent, text=prev)
            last = agent
        emit(type="final", agent=last, text=prev)
    finally:
        busy.clear()

# Реальная активность моделей (вне зависимости от того, откуда пришла задача)
activity = {"claude_app": False, "claude": False, "qwen": False, "deepseek": False}
details = dict.fromkeys(activity, "")  # что агент делает прямо сейчас (короткая реплика для пузыря)

def brief(t, n=120):
    t = " ".join(str(t).split())
    return t if len(t) <= n else t[:n - 1] + "…"

def describe_tool(name, args):
    """Название инструмента + безопасные детали (имя файла, домен). Сами команды не показываем."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    args = args if isinstance(args, dict) else {}
    n = str(name or "").lower()
    if n.startswith("mcp__"):  # внешние инструменты: показываем понятное действие, а не техническое имя
        srv = n.split("__")[1] if n.count("__") >= 2 else ""
        if "browser" in srv or "chrome" in srv:
            return "смотрю в браузере"
        if "terminal" in srv:
            return "работаю в терминале"
        if "computer" in srv:
            return "работаю с экраном"
        if srv.startswith("ccd") or "session" in srv:
            return "управляю приложением"
        return "использую инструмент"
    path = args.get("file_path") or args.get("absolute_path") or args.get("path") or ""
    fname = os.path.basename(str(path)) if path else ""
    if "shell" in n or n == "bash":
        return brief(args.get("description") or "запускаю команду", 90)
    if "search" in n and ("web" in n or "internet" in n):
        return "ищу в интернете"
    if "fetch" in n or n.startswith("web"):
        host = re.sub(r"^https?://([^/]+).*$", r"\1", str(args.get("url") or ""))
        return "открываю страницу " + host if host else "открываю страницу"
    if "read" in n or "list" in n:
        return "читаю " + (fname or "файлы")
    if any(k in n for k in ("edit", "write", "replace")):
        return "пишу " + (fname or "файл")
    if any(k in n for k in ("grep", "glob", "search", "find")):
        return "ищу в файлах"
    if any(k in n for k in ("task", "agent")):
        return "зову помощника"
    if "todo" in n:
        return "веду план работ"
    if "message" in n:
        return "пишу сообщение"
    return brief(name or "работаю", 40)

def last_action(rows, text_of, tool_of):
    """Последнее действие модели по журналу: реплика (текст) или инструмент."""
    last = next((r for r in reversed(rows) if r.get("type") in ("user", "assistant", "tool_result")), None)
    if last is None:
        return ""
    if last["type"] == "user" and text_of(last):
        return "читаю задачу"
    for r in reversed(rows):
        if r.get("type") == "assistant":
            t, tool = text_of(r), tool_of(r)
            if tool:
                return tool
            if t:
                return brief(t)
            return "думаю"
    return ""

def _claude_parts(r):
    c = (r.get("message") or {}).get("content")
    return c if isinstance(c, list) else []

def claude_text(r):
    return " ".join(b.get("text", "") for b in _claude_parts(r) if isinstance(b, dict) and b.get("type") == "text").strip()

def claude_tool(r):
    for b in reversed(_claude_parts(r)):
        if isinstance(b, dict) and b.get("type") == "tool_use":
            return describe_tool(b.get("name"), b.get("input"))
    return ""

def _qwen_parts(r):
    p = (r.get("message") or {}).get("parts")
    return p if isinstance(p, list) else []

def qwen_text(r):
    return " ".join(str(p.get("text", "")) for p in _qwen_parts(r) if isinstance(p, dict) and "text" in p and not p.get("thought")).strip()

def qwen_tool(r):
    for p in reversed(_qwen_parts(r)):
        if isinstance(p, dict) and "functionCall" in p:
            fc = p["functionCall"] or {}
            return describe_tool(fc.get("name"), fc.get("args"))
    return ""
LMS = os.path.expanduser("~/.lmstudio/bin/lms")
CLAUDE_LOGS = os.path.expanduser("~/.claude/projects/**/*.jsonl")

CLAUDE_APP = "/Applications/Claude.app/Contents/MacOS/Claude"
LMS_APP = "/Applications/LM Studio.app/Contents/MacOS/LM Studio"

def is_claude_code(a):
    return "claude.app/Contents/MacOS/claude" in a  # CLI; у приложения Claude.app регистр другой

PPID = {}  # pid -> ppid последнего снимка процессов

def cpu_secs(t):  # "mm:ss.ss" или "h:mm:ss" -> секунды
    sec = 0.0
    for part in t.split(":"):
        sec = sec * 60 + float(part)
    return sec

def processes():
    """[(pid, cpu_секунды, командная строка)] всех процессов, по возрастанию pid."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,cputime=,args="], capture_output=True,
                             text=True, timeout=4).stdout
    except Exception:
        return []
    rows = [l.strip().split(None, 3) for l in out.splitlines() if l.strip()]
    rows = [r for r in rows if len(r) == 4]
    PPID.clear()
    PPID.update({int(r[0]): int(r[1]) for r in rows})
    return sorted((int(r[0]), cpu_secs(r[2]), r[3]) for r in rows)

def first(procs, test):
    return next((str(p) for p, _, a in procs if test(a)), None)

DSH_APP = "DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness"
DSH_SESSIONS = os.path.expanduser("~/.dsh/sessions/*/*/session.v4.jsonl.zstd")
_dsh_cpu = [0.0, 0.0]  # (время, суммарный cpu процессов Harness)

try:
    from compression import zstd  # Python 3.14+: журналы Harness сжаты zstd
except ImportError:
    zstd = None
_dsh_files = {}       # путь журнала -> {"mt": время изменения, "n": прочитано строк, "open": идёт ход}
dsh_called_claude = []  # сюда складываются обнаруженные обращения DeepSeek к Claude
CLAUDE_CALL = re.compile(r"claude\s+(-p|--print)|api\.anthropic|anthropic-version|claude-(haiku|sonnet|opus)", re.I)

def dsh_busy():
    """Читает ТОЛЬКО типы событий журналов DeepSeek Harness (turn/start, turn/end, tool/call).
    Возвращает True, если в какой-то сессии сейчас идёт ход. Заодно замечает, когда DeepSeek
    запускает/зовёт Claude (по свежим вызовам инструментов)."""
    busy, now, detail, newest = False, time.time(), "", 0
    for p in glob.glob(DSH_SESSIONS):
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        st = _dsh_files.get(p)
        if st is None and now - mt > 900:
            continue  # старые журналы не читаем
        if st is None or st["mt"] != mt:
            try:
                with open(p, "rb") as f:
                    lines = zstd.decompress(f.read()).decode("utf8", "replace").splitlines()
            except Exception:
                continue  # файл дописывается в этот момент — попробуем в следующий раз
            fresh = st is not None  # при первом чтении истории звонки не считаем
            st = st or {"n": 0, "open": False, "detail": ""}
            for line in lines[st["n"]:]:
                try:
                    kind = json.loads(line).get("type")
                except ValueError:
                    continue
                if kind == "turn/start":
                    st["open"], st["detail"] = True, "думаю"
                elif kind == "turn/end":
                    st["open"], st["detail"] = False, ""
                elif kind == "tool/call":
                    d = (json.loads(line).get("data") or {})
                    st["detail"] = describe_tool(d.get("name"), d.get("arguments"))
                if kind == "tool/call" and fresh and CLAUDE_CALL.search(line):
                    dsh_called_claude.append(now)
            st["n"], st["mt"] = len(lines), mt
            _dsh_files[p] = st
        if st["open"] and now - mt < 600:
            busy = True
            if mt >= newest:
                newest, detail = mt, st.get("detail", "")
    return busy, detail

def probe():
    """Возвращает (кто сейчас занят, кто в сети: токен процесса или None)."""
    act = dict.fromkeys(activity, False)
    det = {}
    lms_out = ""
    try:  # Qwen: LM Studio показывает статус модели (IDLE / не IDLE)
        lms_out = subprocess.run([LMS, "ps"], capture_output=True, text=True, timeout=4).stdout
        for line in lms_out.splitlines():
            w = line.split()
            if "qwen" in line.lower() and len(w) > 2 and w[2].upper() != "IDLE":
                act["qwen"] = True
                det["qwen"] = "генерирую ответ"
    except Exception:
        pass
    try:  # Qwen Code в терминале (отдельно от LM Studio)
        code_busy, code_det = qwen_code_state()
        if code_busy:
            act["qwen"], det["qwen"] = True, code_det or det.get("qwen", "")
    except Exception:
        pass
    try:  # Claude: в свежем журнале сессии последний ход не завершён (нет end_turn)
        files = glob.glob(CLAUDE_LOGS, recursive=True)
        newest = max(files, key=os.path.getmtime, default=None)
        if newest and time.time() - os.path.getmtime(newest) < 180:
            with open(newest, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 200_000))
                rows = [json.loads(l) for l in f.read().splitlines()[1:] if l.strip()]
            last = next((r for r in reversed(rows) if r.get("type") in ("user", "assistant")), None)
            if last:
                done = last["type"] == "assistant" and last["message"].get("stop_reason") in (
                    "end_turn", "stop_sequence", "max_tokens")
                act["claude"] = not done
                if act["claude"]:
                    det["claude"] = last_action(rows, claude_text, claude_tool)
    except Exception:
        pass
    procs = processes()
    try:  # DeepSeek Harness: по журналам сессий; без zstd — по загрузке процессора
        if zstd:
            act["deepseek"], det["deepseek"] = dsh_busy()
        else:
            total = sum(c for _, c, a in procs if "DeepSeek Harness.app" in a)
            now = time.time()
            rate = (total - _dsh_cpu[1]) / (now - _dsh_cpu[0]) if _dsh_cpu[0] and total >= _dsh_cpu[1] else 0
            _dsh_cpu[:] = [now, total]
            act["deepseek"] = rate > 0.12
    except Exception:
        pass
    # Присутствие: запущена ли программа. Токен меняется при перезапуске (новый pid).
    here = {
        "claude_app": first(procs, lambda a: a == CLAUDE_APP),
        # Claude Code: есть хотя бы один процесс claude (токен общий, чтобы смена сессий не считалась перезапуском)
        "claude": "code" if first(procs, is_claude_code) else None,
        # Qwen «в сети», если загружен в LM Studio ИЛИ запущен Qwen Code; токен меняется при перезапуске любого
        "qwen": ("|".join([((first(procs, lambda a: a == LMS_APP) or "lms") if "qwen" in lms_out.lower() else ""),
                           first(procs, is_qwen_code) or ""]) if ("qwen" in lms_out.lower() or first(procs, is_qwen_code)) else None),
        "deepseek": first(procs, lambda a: a.endswith(DSH_APP))  # приложение DeepSeek Harness
                    or ("api" if AGENTS["deepseek"]["key"] else None)
                    or ("lms" if "deepseek" in lms_out.lower() else None),
    }
    for k in details:
        details[k] = det.get(k, "") if act[k] else ""
    return act, here  # DeepSeek через API: занятость локально не видна

presence = dict.fromkeys(activity)  # agent -> токен запущенной программы или None

def claude_code_from_harness(procs, seen):
    """Новые процессы Claude Code, порождённые DeepSeek Harness: возвращает True при появлении."""
    args = {p: a for p, _, a in procs}
    now_pids = {p for p, _, a in procs if is_claude_code(a)}
    new = now_pids - seen if seen is not None else set()
    hit = False
    for p in new:
        q, hops = PPID.get(p), 0
        while q and hops < 12:
            if "DeepSeek Harness.app" in args.get(q, ""):
                hit = True
            q, hops = PPID.get(q), hops + 1
    return hit, now_pids

QWEN_CHATS = os.path.expanduser("~/.qwen/projects/*/chats/*.jsonl")

def qwen_code_state():
    """Qwen Code (терминал): (идёт ли ход, что делает). Ход идёт, если последнее сообщение — не финальный текст."""
    files = glob.glob(QWEN_CHATS)
    newest = max(files, key=os.path.getmtime, default=None)
    if not newest or time.time() - os.path.getmtime(newest) > 300:
        return False, ""
    with open(newest, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 200_000))
        rows = []
        for l in f.read().splitlines()[1:]:
            try:
                rows.append(json.loads(l))
            except ValueError:
                pass
    last = next((r for r in reversed(rows) if r.get("type") in ("user", "assistant", "tool_result")), None)
    if not last:
        return False, ""
    busy_now = last["type"] != "assistant" or any(
        isinstance(p, dict) and "functionCall" in p for p in _qwen_parts(last))
    return busy_now, (last_action(rows, qwen_text, qwen_tool) if busy_now else "")

def is_qwen_code(a):
    return "node" in a and a.split()[-1].endswith("/qwen")

def who_calls_qwen():
    """Кто сейчас держит соединение с LM Studio (порт 1234): 'claude' / 'deepseek' / None."""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:1234", "-sTCP:ESTABLISHED"],
                             capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return None
    args = {p: a for p, _, a in processes()}
    for line in out.splitlines()[1:]:
        w = line.split()
        if len(w) < 9 or "->127.0.0.1:1234" not in w[8] or not w[8].startswith("127.0.0.1:"):
            continue  # нужны именно клиенты, а не сам LM Studio
        a = args.get(int(w[1]), "")
        if "office.py" in a:
            return None  # свои задачи офис рисует сам
        if "DeepSeek Harness.app" in a:
            return "deepseek"
        if "claude" in a.lower():
            return "claude"
    return None

def monitor():
    misses = dict.fromkeys(activity, 0)
    seen = dict.fromkeys(activity, (None, 0))
    handed = False
    shown = {}
    cc_seen = None
    last_hand = {}
    while True:
        act, here = probe()
        for k, v in act.items():
            if activity[k] != v:
                activity[k] = v
                emit(type="activity", agent=k, active=v)
        for k, v in details.items():
            if shown.get(k) != v:
                shown[k] = v
                emit(type="detail", agent=k, text=v)
        spawned, cc_seen = claude_code_from_harness(processes(), cc_seen)
        if dsh_called_claude or spawned:  # DeepSeek запустил/позвал Claude Code -> передача задачи
            dsh_called_claude.clear()
            if time.time() - last_hand.get("dc", 0) > 10:
                last_hand["dc"] = time.time()
                emit(type="handoff", src="deepseek", dst="claude", text="")
        if act["qwen"] and not handed:  # Qwen взялся за работу: от кого пришла задача?
            src = who_calls_qwen()
            if src:
                handed = True
                emit(type="handoff", src=src, dst="qwen", text="")
        elif not act["qwen"]:
            handed = False
        for k, tok in here.items():
            misses[k] = 0 if tok else misses[k] + 1
            seen[k] = (tok, seen[k][1] + 1) if tok and seen[k][0] == tok else (tok, 1)
            if tok and tok != presence[k] and (presence[k] is None or seen[k][1] >= 2):
                presence[k] = tok  # новая программа/перезапуск (подтверждено двумя замерами)
                emit(type="presence", agent=k, here=True, token=tok)
            elif not tok and presence[k] and misses[k] >= 2:  # 2 промаха подряд = закрыта
                presence[k] = None
                emit(type="presence", agent=k, here=False, token="")
        time.sleep(1.5)

def shutdown():
    emit(type="leave")
    time.sleep(7)  # дать людям дойти до двери
    os._exit(0)

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # нужно для потока частями (chunked) через туннель

    def log_message(self, *a): pass

    def chunk(self, data: bytes):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def is_local(self):
        """Запрос именно с этого компьютера, не через туннель/прокси."""
        h = self.headers
        proxied = any(k in h for k in ("X-Forwarded-For", "Cf-Connecting-Ip", "X-Forwarded-Host", "Forwarded"))
        return (self.client_address[0] in ("127.0.0.1", "::1") and not proxied
                and h.get("Host", "").split(":")[0] in ("localhost", "127.0.0.1"))

    def allowed(self):
        if self.is_local():
            return True
        q = self.path.partition("?")[2]
        return secrets.compare_digest(dict(p.partition("=")[::2] for p in q.split("&") if p).get("key", ""), TOKEN)

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, "нужна ссылка с ключом")
        route = self.path.partition("?")[0]
        if route == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            q = queue.Queue()
            with lock: listeners.append(q)
            try:
                self.chunk(b": hi " + b" " * 2048 + b"\n\n")  # «пробивает» буферы прокси
                for k, v in activity.items():
                    q.put({"type": "activity", "agent": k, "active": v})
                    q.put({"type": "detail", "agent": k, "text": details[k]})
                for k, tok in presence.items():
                    q.put({"type": "presence", "agent": k, "here": bool(tok), "token": tok or ""})
                while True:
                    try:
                        self.chunk(f"data: {json.dumps(q.get(timeout=10))}\n\n".encode())
                    except queue.Empty:
                        self.chunk(b": ping\n\n")
            except OSError:
                pass
            finally:
                with lock: listeners.remove(q)
        elif route == "/poll":
            q = dict(p.partition("=")[::2] for p in self.path.partition("?")[2].split("&") if p)
            try:
                since = int(q.get("since", "-1"))
            except ValueError:
                since = -1
            self.reply(200, json.dumps(poll(since)), "application/json")
        elif route == "/agents":
            self.reply(200, json.dumps({k: {"name": v["name"], "role": v["role"]}
                                        for k, v in AGENTS.items()}), "application/json")
        else:
            self.reply(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        if not self.is_local():  # из интернета — только просмотр
            return self.reply(403, "только просмотр")
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) or b"{}"
        if self.path == "/quit":
            self.reply(200, "bye")
            threading.Thread(target=shutdown, daemon=True).start()
            return
        task = json.loads(raw).get("task", "").strip()
        if not task or busy.is_set():
            return self.reply(409, "busy")
        busy.set()
        threading.Thread(target=run_task, args=(task,), daemon=True).start()
        self.reply(200, "ok")

    def reply(self, code, body, ctype="text/plain"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

# ---- Страница ------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang="ru"><meta charset="utf-8"><title>AI Office 3D</title>
<style>
html,body{height:100%;margin:0}
body{display:flex;flex-direction:column;font-family:-apple-system,sans-serif;background:#2b2f3a;color:#eee}
#bar{display:flex;gap:8px;padding:10px}
#task{flex:1;padding:10px;border-radius:8px;border:0;font-size:15px}
button{padding:10px 18px;border:0;border-radius:8px;background:#f5b942;font-weight:700;cursor:pointer}
#off{background:#e5645a;color:#fff}
button:disabled{opacity:.5}
#view{flex:1;position:relative;overflow:hidden;min-height:0}
#view canvas{display:block}
#log{height:max(150px,22vh);margin:8px 10px 10px;padding:10px;background:#1e2129;border-radius:8px;overflow:auto;font-size:15px;line-height:1.4;white-space:pre-wrap}
.bubble{position:absolute;pointer-events:none;max-width:min(360px,74vw);background:#fff;color:#222;border:3px solid #222;
 border-radius:16px;padding:10px 15px;font:600 17px/1.35 -apple-system,sans-serif;overflow-wrap:anywhere;transform:translate(-50%,-100%);display:none}
.bubble:before,.bubble:after{content:'';position:absolute;left:50%;border:solid transparent}
.bubble:before{bottom:-24px;margin-left:-12px;border-width:12px;border-top-color:#222}
.bubble:after{bottom:-17px;margin-left:-9px;border-width:9px;border-top-color:#fff}
.tag{position:absolute;pointer-events:none;transform:translate(-50%,-100%);text-align:center;color:#fff;
 padding:2px 7px;border-radius:7px;border:1.5px solid rgba(0,0,0,.5);box-shadow:0 1px 4px rgba(0,0,0,.3);font-size:9.5px;line-height:1.15;opacity:.92;white-space:nowrap}
.tag b{display:block;font-size:11.5px}.tag.off{filter:grayscale(1);opacity:.55}
.bubble.think{border-radius:26px;font-style:italic;font-weight:500}
#banner{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
 font-size:28px;font-weight:700;background:rgba(20,22,30,.55);pointer-events:none;text-align:center}
#hint{position:absolute;right:12px;top:8px;font-size:12px;opacity:.7}
</style>
<div id="bar"><input id="task" placeholder="Дайте задачу команде, например: напиши короткое поздравление с днём рождения коллеге">
<button id="go">Отправить</button><button id="off">Выключить офис</button></div>
<div id="view"><div id="banner">Офис закрыт — программа выключена.<br>Запустите office.py снова.</div>
<div id="hint">тяните мышью — вращать, колесо — приблизить</div></div>
<div id="log"></div>
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js"}}</script>
<script type="module">
import * as THREE from 'three';
const KEY=new URLSearchParams(location.search).get('key');
const Q=KEY?'?key='+encodeURIComponent(KEY):'';
const VIEWER=!['localhost','127.0.0.1'].includes(location.hostname);
if(VIEWER){const bar=document.getElementById('bar');[...bar.children].forEach(c=>c.style.display='none');
  const n=document.createElement('div');n.style.cssText='padding:6px 4px;opacity:.85';
  n.textContent='👁 Режим просмотра — офис работает на компьютере владельца';bar.appendChild(n)}
const names=await fetch('/agents'+Q).then(r=>r.json());
const order=['claude_app','claude','qwen','deepseek'];
const COL={claude_app:0xf0b27a,claude:0xd97757,qwen:0x6c5ce7,deepseek:0x2e86de};
const HAIR={claude_app:0x7a4a2a,claude:0x5a3a22,qwen:0x1d1d1d,deepseek:0xb88a3e};
const DX={claude_app:-3.9,claude:-1.3,qwen:1.3,deepseek:3.9};
const SEATZ=-1.62, DOORZ=0.9, DOORX=-6.1;
const $=id=>document.getElementById(id);
const view=$('view'),logEl=$('log');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const frame=()=>new Promise(r=>requestAnimationFrame(r));

// ---------- сцена ----------
const renderer=new THREE.WebGLRenderer({antialias:true});
renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
view.appendChild(renderer.domElement);
const scene=new THREE.Scene();scene.background=new THREE.Color(0x2b2f3a);
const cam=new THREE.PerspectiveCamera(45,1,.1,100);
let az=.5,el=.42,dist=12.5;const tgt=new THREE.Vector3(0,1.2,-1);
const placeCam=()=>{cam.position.set(tgt.x+dist*Math.sin(az)*Math.cos(el),tgt.y+dist*Math.sin(el),tgt.z+dist*Math.cos(az)*Math.cos(el));cam.lookAt(tgt)};
let drag=null;
renderer.domElement.onpointerdown=e=>{drag=[e.clientX,e.clientY]};
addEventListener('pointerup',()=>drag=null);
addEventListener('pointermove',e=>{if(!drag)return;az-=(e.clientX-drag[0])*.005;el=Math.max(.1,Math.min(1.3,el+(e.clientY-drag[1])*.004));drag=[e.clientX,e.clientY];placeCam()});
renderer.domElement.onwheel=e=>{e.preventDefault();dist=Math.max(5,Math.min(22,dist+e.deltaY*.01));placeCam()};
const resize=()=>{const w=view.clientWidth,h=view.clientHeight;renderer.setSize(w,h);cam.aspect=w/h;
  // на узком экране (телефон) расширяем вертикальный угол, чтобы комната помещалась по ширине
  cam.fov=Math.min(100,2*Math.atan(Math.tan(22.5*Math.PI/180)*Math.max(1,1.35/cam.aspect))*180/Math.PI);
  cam.updateProjectionMatrix()};
new ResizeObserver(resize).observe(view);resize();placeCam();

scene.add(new THREE.HemisphereLight(0xffffff,0x887766,.9));
const sun=new THREE.DirectionalLight(0xfff2dd,1.4);sun.position.set(4,9,6);sun.castShadow=true;
Object.assign(sun.shadow.camera,{left:-9,right:9,top:9,bottom:-9,near:1,far:30});sun.shadow.mapSize.set(1024,1024);scene.add(sun);

const std=c=>new THREE.MeshStandardMaterial({color:c,roughness:.75});
function box(w,h,d,c,x,y,z,parent=scene){const m=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),std(c));m.position.set(x,y,z);m.castShadow=m.receiveShadow=true;parent.add(m);return m}

// пол, ковёр, стены
box(12,.2,8,0xb68b5e,0,-.1,0);box(6.5,.02,3.2,0x6b7fa8,0,.01,.2);
box(3,.2,2.2,0x555d6e,-7.6,-.1,DOORZ); // коридор
const WALL=0xe8e0cf;
box(12.4,1.2,.2,WALL,0,.6,-4.1);box(12.4,.9,.2,WALL,0,3.05,-4.1);
box(4.9,1.4,.2,WALL,-3.65,1.9,-4.1);box(4.9,1.4,.2,WALL,3.65,1.9,-4.1);
const LW=0xdcd3bf;
box(.2,3.5,4.3,LW,-6.1,1.75,-1.85);box(.2,3.5,2.6,LW,-6.1,1.75,2.7);box(.2,1.3,1,LW,-6.1,2.85,.9);
box(.1,.1,12.4,0x9b8e75,-6.0,.05,0).scale.z=.65; // плинтус
// окно
const fr=0x7a5b3a;
box(2.6,.08,.12,fr,0,1.2,-4.05);box(2.6,.08,.12,fr,0,2.6,-4.05);box(.08,1.48,.12,fr,-1.2,1.9,-4.05);box(.08,1.48,.12,fr,1.2,1.9,-4.05);
box(.05,1.4,.1,fr,0,1.9,-4.05);box(2.4,.05,.1,fr,0,1.9,-4.05);
box(2.8,.08,.3,0xf2ece0,0,1.16,-3.95);
const skyC=document.createElement('canvas');skyC.width=4;skyC.height=256;const sg=skyC.getContext('2d');
const gr=sg.createLinearGradient(0,0,0,256);gr.addColorStop(0,'#5aa9ee');gr.addColorStop(1,'#cfe9ff');sg.fillStyle=gr;sg.fillRect(0,0,4,256);
const sky=new THREE.Mesh(new THREE.PlaneGeometry(5,2.4),new THREE.MeshBasicMaterial({map:new THREE.CanvasTexture(skyC)}));sky.position.set(0,1.9,-4.6);scene.add(sky);
const sunD=new THREE.Mesh(new THREE.CircleGeometry(.35,24),new THREE.MeshBasicMaterial({color:0xfff3a0}));sunD.position.set(.8,2.3,-4.55);scene.add(sunD);
const clouds=[0,1,2].map(i=>{const g=new THREE.Group();[0,.5,-.5].forEach((o,j)=>{const s=new THREE.Mesh(new THREE.SphereGeometry(.3-j*.04,12,8),new THREE.MeshBasicMaterial({color:0xffffff}));s.position.set(o,j==0?.08:0,0);s.scale.y=.6;g.add(s)});g.position.set(-1.5+i*1.5,1.8+i*.2,-4.5);scene.add(g);return g});
// дверь
const doorPivot=new THREE.Group();doorPivot.position.set(DOORX+.02,0,.4);scene.add(doorPivot);
box(.06,2.2,.96,0x9a6a3c,0,1.1,.48,doorPivot);
const knob=new THREE.Mesh(new THREE.SphereGeometry(.05,10,8),std(0xd4af37));knob.position.set(.05,1.05,.85);doorPivot.add(knob);
box(.1,2.25,.06,0x6b4a2a,DOORX,1.12,.37);box(.1,2.25,.06,0x6b4a2a,DOORX,1.12,1.43);box(.1,.06,1.1,0x6b4a2a,DOORX,2.22,.9);
// часы
const clock=new THREE.Group();clock.position.set(4.2,2.7,-3.95);scene.add(clock);
const cf=new THREE.Mesh(new THREE.CylinderGeometry(.35,.35,.05,32),std(0xffffff));cf.rotation.x=Math.PI/2;clock.add(cf);
const hand=(l,w)=>{const g=new THREE.Group();const b=box(w,l,.02,0x222222,0,l/2,.04,g);clock.add(g);return g};
const hH=hand(.17,.025),hM=hand(.26,.015);
// растения
function plant(x,z){const g=new THREE.Group();g.position.set(x,0,z);scene.add(g);
  const p=new THREE.Mesh(new THREE.CylinderGeometry(.22,.17,.4,16),std(0xb5651d));p.position.y=.2;p.castShadow=true;g.add(p);
  [[0,.75,0,.3],[.15,.6,.1,.2],[-.15,.65,-.1,.22]].forEach(([a,b,c,r])=>{const l=new THREE.Mesh(new THREE.SphereGeometry(r,12,10),std(0x3f8f4a));l.position.set(a,b,c);l.castShadow=true;g.add(l)})}
plant(5.5,-3.5);plant(-5.5,-3.5);

// ---------- столы, стулья, мониторы ----------
const monitors={};
function makeStation(id){
  const x=DX[id],g=new THREE.Group();g.position.set(x,0,-2.7);scene.add(g);
  box(1.9,.05,1.4,0xc9a574,0,.775,0,g);
  box(.05,.75,1.3,0x8a6a45,-.9,.375,0,g);box(.05,.75,1.3,0x8a6a45,.9,.375,0,g);box(1.8,.4,.03,0x8a6a45,0,.5,-.62,g);
  // монитор
  box(.3,.02,.2,0x333333,0,.81,-.35,g);box(.06,.25,.05,0x333333,0,.93,-.35,g);
  box(.78,.48,.05,0x222222,0,1.2,-.38,g);
  const c=document.createElement('canvas');c.width=160;c.height=96;const ctx=c.getContext('2d'),tex=new THREE.CanvasTexture(c);
  const scr=new THREE.Mesh(new THREE.PlaneGeometry(.7,.4),new THREE.MeshBasicMaterial({map:tex}));scr.position.set(0,1.2,-.35);g.add(scr);
  monitors[id]={c,ctx,tex,t:0,off:0};
  box(.45,.02,.15,0x444444,0,.81,.35,g);box(.07,.03,.1,0x444444,.38,.81,.35,g);
  const mug=new THREE.Mesh(new THREE.CylinderGeometry(.05,.05,.1,12),std(COL[id]));mug.position.set(.65,.85,.1);g.add(mug);
  // табличка с именем
  const nc=document.createElement('canvas');nc.width=256;nc.height=64;const n=nc.getContext('2d');
  n.fillStyle='#fff';n.fillRect(0,0,256,64);n.fillStyle='#'+COL[id].toString(16).padStart(6,'0');n.fillRect(0,0,256,10);
  n.fillStyle='#222';n.font='bold 30px sans-serif';n.textAlign='center';n.fillText(names[id].name,128,46);
  const pl=new THREE.Mesh(new THREE.PlaneGeometry(.4,.1),new THREE.MeshBasicMaterial({map:new THREE.CanvasTexture(nc)}));pl.position.set(-.55,.84,.62);pl.rotation.x=-.6;g.add(pl);
  // стул
  const ch=new THREE.Group();ch.position.set(x,0,-1.65);scene.add(ch);
  const cc=0x3a3f4d;box(.5,.07,.5,cc,0,.44,0,ch);const bk=box(.46,.5,.06,cc,0,.75,.25,ch);bk.rotation.x=.12;
  box(.06,.3,.06,0x888888,0,.25,0,ch);
  for(let i=0;i<5;i++){const a=i*Math.PI*2/5;const l=box(.28,.04,.05,0x555555,Math.sin(a)*.14,.08,Math.cos(a)*.14,ch);l.rotation.y=a+Math.PI/2;
    const w=new THREE.Mesh(new THREE.SphereGeometry(.035,8,6),std(0x222222));w.position.set(Math.sin(a)*.28,.04,Math.cos(a)*.28);ch.add(w)}
}
order.forEach(makeStation);

// ---------- человечки ----------
const KEYS=['py','pz','tx','hx','lt','ls','rt','rs','lux','luz','lfx','lfz','rux','ruz','rfx','rfz'];
function makePerson(id){
  const skin=0xffd2a8,col=COL[id];
  const root=new THREE.Group();root.visible=false;scene.add(root);
  const pelvis=new THREE.Group();root.add(pelvis);
  const M=(geo,c,par,x=0,y=0,z=0)=>{const o=new THREE.Mesh(geo,std(c));o.position.set(x,y,z);o.castShadow=true;par.add(o);return o};
  M(new THREE.BoxGeometry(.34,.18,.2),0x2b3345,pelvis);
  const torso=new THREE.Group();pelvis.add(torso);
  M(new THREE.CapsuleGeometry(.17,.3,6,12),col,torso,0,.3,0).scale.z=.7;
  const neck=new THREE.Group();neck.position.set(0,.62,0);torso.add(neck);
  M(new THREE.SphereGeometry(.13,18,14),skin,neck,0,.14,0);
  M(new THREE.SphereGeometry(.14,18,12,0,Math.PI*2,0,Math.PI*.55),HAIR[id],neck,0,.16,-.02);
  [-.05,.05].forEach(x=>M(new THREE.SphereGeometry(.018,8,6),0x111111,neck,x,.15,.118));
  const j={root,pelvis,torso,neck,l:{},r:{},id,paper:null};
  for(const [s,k] of [[-1,'l'],[1,'r']]){
    const up=new THREE.Group();up.position.set(s*.23,.52,0);torso.add(up);
    M(new THREE.CapsuleGeometry(.05,.22,4,8),col,up,0,-.17,0);
    const fo=new THREE.Group();fo.position.set(0,-.34,0);up.add(fo);
    M(new THREE.CapsuleGeometry(.045,.2,4,8),skin,fo,0,-.16,0);M(new THREE.SphereGeometry(.05,10,8),skin,fo,0,-.32,0);
    const hip=new THREE.Group();hip.position.set(s*.1,-.02,0);pelvis.add(hip);
    M(new THREE.CapsuleGeometry(.075,.3,4,8),0x2b3345,hip,0,-.23,0);
    const sh=new THREE.Group();sh.position.set(0,-.46,0);hip.add(sh);
    M(new THREE.CapsuleGeometry(.065,.3,4,8),0x2b3345,sh,0,-.23,0);M(new THREE.BoxGeometry(.1,.06,.24),0x222222,sh,0,-.455,.06);
    j[k]={up,fo,hip,sh};
    if(k==='r'){const p=M(new THREE.BoxGeometry(.24,.01,.32),0xffffff,fo,0,-.34,.12);p.visible=false;j.paper=p}
  }
  return Object.assign(j,{pose:Object.fromEntries(KEYS.map(k=>[k,0])),mode:'stand',ph:0,moving:false,yawT:0,job:0,carry:false,seated:false,bubble:null,kind:'',pw:false,ext:false,detail:'',in:false,here:false,tok:''});
}
const P={};order.forEach(id=>P[id]=makePerson(id));

const arm=(s,ux,uz,fx,fz)=>({ux,uz:s*uz,fx,fz:s*fz});
function poseFor(p,t){
  const o={py:.95,pz:0,tx:0,hx:0,lt:0,ls:0,rt:0,rs:0,lux:0,luz:-.1,lfx:-.15,lfz:0,rux:0,ruz:.1,rfx:-.15,rfz:0};
  const set=(k,v)=>{o[k]=v};
  switch(p.mode){
  case 'walk':case 'walkc':{const sw=Math.sin(p.ph)*.55;o.py=.955+Math.abs(Math.cos(p.ph))*.02;o.lt=sw;o.rt=-sw;
    o.ls=Math.max(0,-Math.cos(p.ph))*.8;o.rs=Math.max(0,Math.cos(p.ph))*.8;o.lux=-sw*.6;o.rux=sw*.6;
    if(p.mode==='walkc'){o.rux=-1.2;o.rfx=-.5;o.ruz=.15}break}
  case 'hand':o.rux=-1.35;o.rfx=-.3;o.ruz=.1;o.tx=.05;break;
  case 'talk':o.rux=-.6;o.rfx=-1.0+Math.sin(t*6)*.25;o.rfz=-.2;break;
  case 'work':{Object.assign(o,{py:.58,tx:.12,hx:.3+Math.sin(t*.7)*.03,lt:-1.35,ls:1.35,rt:-1.35,rs:1.35});
    Object.assign(o,{lux:-.15,luz:-.15,lfx:-1.3+Math.sin(t*13)*.07,lfz:.2,rux:-.15,ruz:.15,rfx:-1.3+Math.cos(t*11)*.07,rfz:-.2});break}
  case 'rest':{Object.assign(o,{py:.58,pz:.06,tx:-.32+Math.sin(t*1.1)*.02,hx:-.1,lt:-1.45,ls:1.15,rt:-1.4,rs:1.1});
    Object.assign(o,{lux:-.5,luz:-2.2,lfx:-.2,lfz:-2.0,rux:-.5,ruz:2.2,rfx:-.2,rfz:2.0});break}
  }
  return o;
}
let DT=.016;const angD=(a,b)=>{let d=(b-a)%(Math.PI*2);if(d>Math.PI)d-=Math.PI*2;if(d<-Math.PI)d+=Math.PI*2;return d};
function updPerson(p,t){
  if(p.seated){p.mode=(p.pw||p.ext)?'work':'rest';
    const working=p.pw||p.ext;
    if(working&&(!p.text||p.kind==='work'||p.kind==='think')){const want=p.detail||(p.pw?'Хмм':'Работаю');
      if(p.text!==want||p.kind!=='work')say(p,want,'work')}
    else if(!working&&(p.kind==='work'||p.kind==='think'))say(p,'')}
  const tg=poseFor(p,t),k=1-Math.exp(-DT*8);
  for(const key of KEYS)p.pose[key]+=(tg[key]-p.pose[key])*k;
  const s=p.pose;p.pelvis.position.set(0,s.py,s.pz);p.torso.rotation.x=s.tx;p.neck.rotation.x=s.hx;
  p.l.hip.rotation.x=s.lt;p.l.sh.rotation.x=s.ls;p.r.hip.rotation.x=s.rt;p.r.sh.rotation.x=s.rs;
  p.l.up.rotation.set(s.lux,0,s.luz);p.l.fo.rotation.set(s.lfx,0,s.lfz);p.r.up.rotation.set(s.rux,0,s.ruz);p.r.fo.rotation.set(s.rfx,0,s.rfz);
  p.root.rotation.y+=angD(p.root.rotation.y,p.yawT)*(1-Math.exp(-DT*9));
  if(p.moving)p.ph+=DT*8;
  p.paper.visible=p.carry;
}
// ---------- движение ----------
class Cancel extends Error{}
const chk=(p,id)=>{if(p.job!==id)throw new Cancel()};
async function goto(p,id,x,z,spd=1.8){
  p.moving=true;p.mode=p.carry?'walkc':'walk';p.seated=false;
  for(;;){chk(p,id);const dx=x-p.root.position.x,dz=z-p.root.position.z,d=Math.hypot(dx,dz);if(d<.04)break;
    p.yawT=Math.atan2(dx,dz);const st=Math.min(d,spd*DT);p.root.position.x+=dx/d*st;p.root.position.z+=dz/d*st;await frame()}
  p.moving=false;
}
async function wait(p,id,ms){const end=performance.now()+ms;while(performance.now()<end){chk(p,id);await frame()}}
async function sitDown(p,id,mode='rest'){p.yawT=Math.PI;p.moving=false;p.mode='stand';await wait(p,id,250);p.mode=mode;p.seated=true;p.kind='';}
async function enter(p,delay){const id=++p.job;try{
  await wait(p,id,delay);const x=DX[p.id];p.root.visible=true;p.root.position.set(-7.6,0,DOORZ);p.root.rotation.y=p.yawT=Math.PI/2;
  p.mode='stand';p.carry=false;say(p,'');
  await goto(p,id,-5.0,DOORZ);await goto(p,id,x,-.6);await goto(p,id,x,SEATZ,1.2);await sitDown(p,id,'rest');
}catch(e){if(!(e instanceof Cancel))throw e}}
async function leave(p,delay){const id=++p.job;try{
  say(p,'');p.carry=false;await wait(p,id,delay);
  if(!p.root.visible)return;const x=DX[p.id];
  p.seated=false;p.moving=false;p.mode='stand';await wait(p,id,700);
  await goto(p,id,x,-.7,1.2);await goto(p,id,-5.0,DOORZ);await goto(p,id,-7.6,DOORZ);
  p.root.visible=false;p.moving=false;
}catch(e){if(!(e instanceof Cancel))throw e}}

// ---------- пузыри ----------
for(const id of order){const b=document.createElement('div');b.className='bubble';view.appendChild(b);P[id].bubble=b;
  const t=document.createElement('div');t.className='tag';t.style.background='#'+COL[id].toString(16).padStart(6,'0');
  t.innerHTML='<b>'+names[id].name+'</b><span></span>';view.appendChild(t);P[id].tag=t}
function say(p,text,kind='say'){p.text=text;p.kind=kind;p.bubble.className='bubble'+(kind==='think'||kind==='work'?' think':'');p.bubble.style.display=text?'block':'none'}
const short=t=>{t=t.replace(/\s+/g,' ');return t.length>230?t.slice(0,227)+'…':t};
const _v=new THREE.Vector3();
function placeBubbles(t){const W=view.clientWidth,H=view.clientHeight;
  order.forEach((id,i)=>{const p=P[id];_v.set(DX[id],1.75+(i%2)*.5,-3.0).project(cam);
    p.tag.style.left=((_v.x*.5+.5)*W)+'px';p.tag.style.top=((-_v.y*.5+.5)*H)+'px';
    const st=!p.in?'не запущен':(p.mode==='work'?'работает':'отдыхает');
    p.tag.classList.toggle('off',!p.in);p.tag.lastChild.textContent=st});
  for(const id of order){const p=P[id];if(!p.text||!p.root.visible){p.bubble.style.display='none';continue}
    p.bubble.style.display='block';
    if(p.kind==='think')p.bubble.textContent=p.text+'.'.repeat(1+Math.floor(t*2.5)%3);else p.bubble.textContent=p.text;
    p.neck.getWorldPosition(_v);_v.y+=.5;_v.project(cam);
    p.bubble.style.left=((_v.x*.5+.5)*W)+'px';p.bubble.style.top=((-_v.y*.5+.5)*H-14)+'px'}}

// ---------- события ----------
function log(s){logEl.textContent+=s+'\n\n';logEl.scrollTop=1e9}
const q=[];let pumping=false;
async function pump(){if(pumping)return;pumping=true;while(q.length){try{await handle(q.shift())}catch(e){if(!(e instanceof Cancel))console.error(e)}}pumping=false}
async function handle(e){
  if(e.type==='working'){const p=P[e.agent];p.pw=true;say(p,'Хмм','think')}
  else if(e.type==='done'){const p=P[e.agent];p.pw=false;say(p,short(e.text));log('['+names[e.agent].name+'] '+e.text);await sleep(1500)}
  else if(e.type==='handoff'){const s=P[e.src],d=P[e.dst];if(!s.in||!d.in)return;const id=++s.job,sx=DX[e.src],dx=DX[e.dst];
    say(s,'Держи, '+names[e.dst].name+'!');s.mode='stand';s.seated=false;await wait(s,id,500);s.carry=true;
    await goto(s,id,sx,-.6,1.2);await goto(s,id,dx+(sx<dx?-.9:.9),-.6);await goto(s,id,dx+(sx<dx?-.9:.9),-1.0,1.2);
    s.yawT=Math.atan2(dx-s.root.position.x,-1.7-s.root.position.z);s.mode='hand';await wait(s,id,800);
    s.carry=false;say(d,'Принял, спасибо!');say(s,'');await wait(s,id,900);say(d,'');
    await goto(s,id,sx+(sx<dx?.0:0),-.6);await goto(s,id,sx,SEATZ,1.2);await sitDown(s,id,'rest')}
  else if(e.type==='error'){const p=P[e.agent];p.pw=false;say(p,'Ой, ошибка: '+e.text);log('ОШИБКА '+e.agent+': '+e.text);$('go').disabled=false}
  else if(e.type==='final'){order.forEach(i=>say(P[i],''));const p=P[e.agent];say(p,'Готово! Всё в логе ↓');$('go').disabled=false}
}
// ---------- присутствие ----------
// Человек в комнате, если сервер офиса жив И его программа запущена.
let conn=false;
function sync(id,delay=0){const p=P[id],want=conn&&p.here;
  if(want&&!p.in){p.in=true;p.pw=false;enter(p,delay)}
  else if(!want&&p.in){p.in=false;leave(p,delay)}}
function syncAll(){order.forEach((id,i)=>sync(id,i*900));
  setTimeout(()=>{$('banner').style.display=(!conn)?'flex':'none'},conn?0:4500)}
function onOpen(){conn=true;$('banner').style.display='none';syncAll()}
function onLost(){if(!conn)return;conn=false;q.length=0;$('go').disabled=false;syncAll()}
function onEvent(e){
  if(e.type==='leave'){conn=false;q.length=0;syncAll()}
  else if(e.type==='activity')P[e.agent].ext=e.active;
  else if(e.type==='detail')P[e.agent].detail=e.text;
  else if(e.type==='presence'){const p=P[e.agent],restart=p.here&&e.here&&p.tok!==e.token&&p.in;
    p.here=e.here;p.tok=e.token;
    if(restart){log('Перезапуск: '+names[e.agent].name);p.in=false;leave(p,0).then(()=>{if(conn&&p.here&&!p.in){p.in=true;p.pw=false;enter(p,500)}})}
    else sync(e.agent)}
  else{q.push(e);pump()}}
if(VIEWER){ // через туннель SSE не работает — обычные запросы с ожиданием
  (async()=>{let id=-1;for(;;){try{
    const r=await fetch('/poll?since='+id+(KEY?'&key='+encodeURIComponent(KEY):''));
    if(!r.ok)throw new Error(r.status);const d=await r.json();
    if(id<0)onOpen();id=d.id;d.events.forEach(onEvent)
  }catch(err){onLost();id=-1;await sleep(3000)}}})();
}else{
  const es=new EventSource('/events'+Q);
  es.onopen=onOpen;es.onerror=onLost;es.onmessage=m=>onEvent(JSON.parse(m.data));
}
async function send(){const t=$('task').value.trim();if(!t||!conn)return;
  const r=await fetch('/task',{method:'POST',body:JSON.stringify({task:t})});
  if(r.ok){$('go').disabled=true;log('ЗАДАЧА: '+t);$('task').value=''}}
$('go').onclick=send;$('task').onkeydown=e=>{if(e.key==='Enter')send()};
$('off').onclick=()=>fetch('/quit',{method:'POST',body:'{}'}).catch(()=>{});

// ---------- цикл ----------
function drawMonitor(id,t){const m=monitors[id],p=P[id],on=p.mode==='work'&&p.seated!==undefined&&p.root.visible;
  if(t-m.t<.12)return;m.t=t;const c=m.ctx;
  c.fillStyle=on?'#0d1b2a':'#05080c';c.fillRect(0,0,160,96);
  if(on){m.off=(m.off+1)%12;for(let i=0;i<9;i++){const w=20+((i*37+m.off*13)%90),ind=((i+m.off)%3)*10;
    c.fillStyle=['#7fd1ff','#9be58f','#f7c873'][(i+m.off)%3];c.fillRect(8+ind,6+i*10,w,5)}}
  m.tex.needsUpdate=true}
const clk=new THREE.Clock();
renderer.setAnimationLoop(()=>{
  DT=Math.min(clk.getDelta(),.3);const t=clk.elapsedTime;
  order.forEach(id=>{updPerson(P[id],t);drawMonitor(id,t)});
  let near=false;order.forEach(id=>{const r=P[id].root;if(r.visible&&r.position.x<-4.3&&Math.abs(r.position.z-DOORZ)<1.2)near=true});
  doorPivot.rotation.y+=((near?1.45:0)-doorPivot.rotation.y)*(1-Math.exp(-DT*5));
  clouds.forEach(c=>{c.position.x+=DT*.12;if(c.position.x>2.3)c.position.x=-2.3});
  const d=new Date();hM.rotation.z=-d.getMinutes()/60*Math.PI*2;hH.rotation.z=-(d.getHours()%12+d.getMinutes()/60)/12*Math.PI*2;
  placeBubbles(t);renderer.render(scene,cam)});
</script></html>"""

def share():
    """--share: поднять туннель Cloudflare и напечатать ссылку для просмотра онлайн."""
    try:
        p = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                             stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("cloudflared не установлен: brew install cloudflared")
        return
    for line in p.stderr:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            print(f"\nСсылка для просмотра онлайн (только просмотр):\n  {m.group(0)}/?key={TOKEN}\n", flush=True)
            break
    for _ in p.stderr:  # держим трубу открытой, пока жив процесс
        pass

if __name__ == "__main__":
    if DEMO:
        presence.update({k: "demo" for k in presence})
    else:
        threading.Thread(target=monitor, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    if "--share" in sys.argv:
        threading.Thread(target=share, daemon=True).start()
    print(f"AI Office 3D: http://localhost:{PORT}" + ("  (demo)" if DEMO else ""))
    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Выключаю офис, сотрудники расходятся по домам...")
        emit(type="leave")
        time.sleep(7)
