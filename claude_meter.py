"""Claude Meter - Claude Code 토큰 사용량을 화면 위에 띄워 두는 작은 위젯.

데이터: ~/.claude/projects/**/*.jsonl (Claude Code 가 응답마다 남기는 usage).
네트워크·API 키 없이 로컬 로그만 읽는다. claude.ai 웹/데스크톱 채팅은 로그가 없어 잡히지 않는다.

표시
  1. 현재 세션 컨텍스트 (마지막 메인 응답의 input + cache_creation + cache_read)
  2. 오늘 누적 토큰 · API 단가 환산 비용
  3. 플랜 한도 사용률(5시간·주간). 실패하면 로그로 추정한 5시간 블록 토큰 · 남은 시간
  4. 활성 세션이 2개 이상이면 세션#1, 세션#2, 기타 N개 (마우스를 올리면 세션 제목, 없으면 작업 폴더 이름)
  줄마다 마우스를 올리면 툴팁: 세션 비용·응답·툴 호출 / 모델별 $·서브에이전트 비중 / 5h 소진 예측 / 주간 페이스

한도 사용률은 Claude Code 로그인 토큰(~/.claude/.credentials.json)으로 /usage 와 같은 비공개 엔드포인트를
120초마다 읽는다(429 시 백오프, 응답은 claude_meter_usage.json 에 캐시). 토큰을 갱신하지는 않는다(Claude Code 로그인이 꼬일 수 있음). 만료되면 Claude Code 가 갱신할 때까지 추정값을 쓴다.

조작: 왼쪽 드래그 = 이동, 더블클릭 = 접기/펴기, 오른쪽 클릭 = 메뉴.
"""
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
from datetime import datetime, timedelta, timezone

APP = "Claude Meter"
BLOCK = timedelta(hours=5)
KEEP = timedelta(hours=30)  # 오늘 + 5시간 블록 계산에 필요한 만큼만 메모리에 둔다

# USD / 1M tokens: (input, output, cache_read). 캐시 쓰기는 input x1.25(5분) / x2(1시간).
# 출처: Anthropic 공개 API 단가(2026-09). 구독 플랜 사용자는 실제 청구액이 아닌 참고값이다.
PRICES = {
    "claude-fable-5": (10.0, 50.0, 0.25),
    "claude-mythos-5": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-4": (1.0, 5.0, 0.10),
}
DEFAULTS = {
    "x": None, "y": None, "alpha": 0.88, "compact": False, "topmost": True,
    "refresh_sec": 5, "warn_ctx": 200_000, "stop_ctx": 300_000,
    "lang": "ko", "claude_dir": None, "prices": {}, "plan_usage": True, "plan_refresh_sec": 120, "limit_red": 80,
    "tool_budget": 150,
}
TEXT = {
    "ko": {"ctx": "컨텍스트", "today": "오늘", "block": "5h 블록", "left": "남음", "idle": "블록 없음",
           "limit": "한도", "reset": "리셋", "week": "주간", "session": "세션", "others": "기타", "count": "개",
           "sessions": "활성", "refresh": "새로고침", "top": "항상 위", "opacity": "투명도",
           "lang": "English", "quit": "종료", "none": "세션 없음",
           "t_cost": "비용  ${:.2f}  (서브에이전트 포함)", "t_resp": "응답 {} · 툴 호출 {} / {}",
           "t_last": "마지막 응답  {} · {}", "t_sub": "서브에이전트  {:.0f}%  (${:.2f})",
           "t_reset": "리셋까지  {}  ({})", "t_fetched": "마지막 조회  {}",
           "t_eta": "소진 예측  {}", "t_safe": "리셋 전 소진 없음 (리셋 때 약 {:.0f}%)", "t_flat": "증가 없음",
           "t_pace": "페이스  {} {:.0f}%p  (지금 기대치 {:.0f}%)", "t_ahead": "여유", "t_over": "초과",
           "t_proj": "이 속도면 리셋 때 약 {:.0f}%", "now": "방금", "ago": "{} 전",
           "d": "{}일 {}시간", "h": "{}시간 {}분", "m": "{}분"},
    "en": {"ctx": "Context", "today": "Today", "block": "5h block", "left": "left", "idle": "no block",
           "limit": "Limit", "reset": "reset", "week": "week", "session": "Session", "others": "Others", "count": "",
           "sessions": "active", "refresh": "Refresh", "top": "Always on top", "opacity": "Opacity",
           "lang": "한국어", "quit": "Quit", "none": "no session",
           "t_cost": "Cost  ${:.2f}  (incl. subagents)", "t_resp": "Replies {} · tool calls {} / {}",
           "t_last": "Last reply  {} · {}", "t_sub": "Subagents  {:.0f}%  (${:.2f})",
           "t_reset": "Resets in  {}  ({})", "t_fetched": "Last fetched  {}",
           "t_eta": "Hits 100%  {}", "t_safe": "not before reset (≈{:.0f}% at reset)", "t_flat": "no growth",
           "t_pace": "Pace  {} {:.0f}%p  (expected now {:.0f}%)", "t_ahead": "under by", "t_over": "over by",
           "t_proj": "At this pace ≈{:.0f}% at reset", "now": "just now", "ago": "{} ago",
           "d": "{}d {}h", "h": "{}h {}m", "m": "{}m"},
}
COLORS = {"bg": "#1b1d23", "fg": "#e6e6e6", "dim": "#8a8f98",
          "ok": "#4cc38a", "warn": "#f5b83d", "stop": "#ef5b5b"}


def app_dir():
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))


def price_of(model, overrides):
    table = {**PRICES, **{k: tuple(v) for k, v in overrides.items()}}
    for prefix in sorted(table, key=len, reverse=True):
        if model.startswith(prefix):
            return table[prefix]
    return None


def cost_of(model, u, overrides):
    p = price_of(model, overrides)
    if not p:
        return 0.0
    inp, out, read = p
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0)
    w5m = u.get("cache_creation_input_tokens", 0) - w1h
    return (u.get("input_tokens", 0) * inp + u.get("output_tokens", 0) * out
            + w5m * inp * 1.25 + w1h * inp * 2 + u.get("cache_read_input_tokens", 0) * read) / 1e6


def total_tokens(u):
    return (u.get("input_tokens", 0) + u.get("output_tokens", 0)
            + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0))


class Ledger:
    """jsonl 을 증분으로 읽어 응답 단위 사용량을 모은다.

    한 응답은 content 블록 수만큼 여러 줄로 기록되고 줄마다 같은 usage 가 반복되므로
    message.id 로 중복을 제거한다. 파일별로 마지막으로 읽은 위치를 기억해 새 줄만 읽는다.
    """

    def __init__(self, root):
        self.root = root
        self.offsets = {}   # path -> byte offset
        self.ctx = {}       # main session path -> (ts, context tokens)
        self.cwd = {}       # main session path -> 작업 폴더
        self.start = {}     # main session path -> 첫 응답 시각
        self.title = {}     # main session path -> Claude Code 가 붙인 세션 제목(ai-title)
        self.events = {}    # message id -> (datetime, model, usage, is_sub)
        self.msgs = {}      # main session path -> {message id: (model, usage, is_sub)}  서브에이전트 포함
        self.tools = {}     # main session path -> 툴 호출(tool_use id) 집합
        self.last = {}      # main session path -> (마지막 응답 시각, 모델)

    def scan(self):
        cutoff = time.time() - KEEP.total_seconds()
        for path in glob.glob(os.path.join(self.root, "**", "*.jsonl"), recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            start = self.offsets.get(path, 0)
            if st.st_size < start:  # 파일이 잘렸거나 교체됨
                start = 0
            if st.st_size == start:
                continue
            self.offsets[path] = self._read(path, start, os.sep + "subagents" + os.sep in path)
        horizon = datetime.now(timezone.utc) - KEEP
        self.events = {k: v for k, v in self.events.items() if v[0] >= horizon}

    def _read(self, path, start, is_sub):
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
        end = data.rfind(b"\n") + 1  # 쓰는 중인 마지막 줄은 다음 번에 읽는다
        for raw in data[:end].splitlines():
            if b'"ai-title"' in raw and not is_sub:  # 제목은 대화가 진행되며 바뀔 수 있어 마지막 값을 쓴다
                try:
                    self.title[path] = json.loads(raw).get("aiTitle") or self.title.get(path)
                except ValueError:
                    pass
                continue
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            msg = d.get("message") or {}
            u, mid, model = msg.get("usage"), msg.get("id"), msg.get("model", "")
            if d.get("type") != "assistant" or not u or not mid or model.startswith("<"):
                continue
            try:
                ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            side = is_sub or bool(d.get("isSidechain"))
            self.events[mid] = (ts, model, u, side)
            # 서브에이전트 로그(<sid>/subagents/*.jsonl)는 부모 세션 <sid>.jsonl 비용에 합산한다
            main = os.path.dirname(os.path.dirname(path)) + ".jsonl" if is_sub else path
            self.msgs.setdefault(main, {})[mid] = (model, u, side)
            if not side:
                self.last[path] = (ts, model)
                tools = self.tools.setdefault(path, set())
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        tools.add(c.get("id"))
                if not self.cwd.get(path):  # 작업 중 cd 해도 세션 이름은 시작 폴더로 고정
                    self.cwd[path] = d.get("cwd")
                    self.start[path] = ts
                self.ctx[path] = (ts, u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                                  + u.get("cache_read_input_tokens", 0))
        return start + end

    def summary(self, overrides):
        now = datetime.now(timezone.utc)
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        evs = sorted(self.events.values(), key=lambda e: e[0])

        today_tok = sum(total_tokens(u) for ts, _, u, _ in evs if ts >= midnight)
        models, sub_cost = {}, 0.0
        for ts, m, u, side in evs:
            if ts >= midnight:
                c = cost_of(m, u, overrides)
                models[short_model(m)] = models.get(short_model(m), 0.0) + c
                sub_cost += c if side else 0.0
        today_cost = sum(models.values())

        # 5시간 블록: 첫 메시지 시각을 정시로 내림한 지점부터 5시간. 그 뒤 첫 메시지가 새 블록을 연다.
        block_start, block_tok = None, 0
        for ts, _, u, _ in evs:
            if block_start is None or ts >= block_start + BLOCK:
                block_start, block_tok = ts.replace(minute=0, second=0, microsecond=0), 0
            block_tok += total_tokens(u)
        if block_start is None or now >= block_start + BLOCK:
            block_left, block_tok = None, 0
        else:
            block_left = block_start + BLOCK - now

        live = sorted(((ts, c, self._name(p)) for p, (ts, c) in self.ctx.items()
                       if now - ts < timedelta(minutes=10)), reverse=True)
        latest = max(self.ctx, key=lambda p: self.ctx[p][0], default=None)
        return {"ctx": self.ctx[latest][1] if latest else None, "active": len(live), "sessions": live,
                "cur": self._session(latest, overrides) if latest else None,
                "today_tok": today_tok, "today_cost": today_cost, "models": models, "sub_cost": sub_cost,
                "block_tok": block_tok, "block_left": block_left}

    def _name(self, path):
        # 대부분 홈 폴더에서 시작해 폴더 이름(~)만으로는 구분이 안 되므로 세션 제목을 먼저 쓴다
        name = self.title.get(path) or session_name(self.cwd.get(path))
        return f"{name}  ({self.start[path].astimezone():%H:%M}~)"

    def _session(self, path, overrides):
        msgs = self.msgs.get(path, {}).values()
        last_ts, model = self.last.get(path, (None, ""))
        return {"name": self._name(path),
                "cost": sum(cost_of(m, u, overrides) for m, u, _ in msgs),
                "replies": sum(1 for *_, side in msgs if not side),
                "tools": len(self.tools.get(path, ())), "last": last_ts, "model": short_model(model)}


def short_model(model):
    # claude-haiku-4-5-20251001 -> haiku-4-5
    return re.sub(r"-\d{8}$", "", model.removeprefix("claude-"))


def session_name(cwd):
    if not cwd:
        return "?"
    if os.path.normcase(os.path.normpath(cwd)) == os.path.normcase(os.path.expanduser("~")):
        return "~"
    return os.path.basename(os.path.normpath(cwd))


def to_local(iso):
    # 서버 값이 02:59:59.9 처럼 올 때가 있어 분 단위로 반올림한다
    return (datetime.fromisoformat(iso) + timedelta(seconds=30)).astimezone() if iso else None


class PlanUsage:
    """플랜 한도 사용률을 백그라운드 스레드에서 가져온다.

    이 엔드포인트는 호출 허용량이 빡빡해(429) 세 가지로 아낀다:
    마지막 응답을 파일에 캐시해 재시작해도 주기 전에는 다시 부르지 않고,
    429 를 받으면 대기 간격을 두 배씩(최대 15분) 늘리고,
    실패해도 30분 안에 받은 값은 계속 보여준다.
    """

    URL = "https://api.anthropic.com/api/oauth/usage"
    STALE = 30 * 60
    MAX_WAIT = 15 * 60

    def __init__(self, interval, cache_path):
        self.interval, self.cache_path = interval, cache_path
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        self.cred = os.path.join(base, ".credentials.json")
        self.raw, self.at, self.samples = None, 0.0, []  # samples: [조회 시각, 5h %, 주간 %] 소진 예측용
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            self.raw, self.at = c["raw"], c["at"]
            self.samples = c.get("samples") or []
        except (OSError, ValueError, KeyError):
            pass
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def data(self):
        if not self.raw or time.time() - self.at > self.STALE:
            return None
        five, week = self.raw.get("five_hour") or {}, self.raw.get("seven_day") or {}
        if five.get("utilization") is None:
            return None
        return {"five": five["utilization"], "week": week.get("utilization"), "at": self.at,
                "reset": to_local(five.get("resets_at")), "week_reset": to_local(week.get("resets_at"))}

    def forecast(self, d):
        """5h 사용률이 100% 에 닿는 시각. 최근 1시간 샘플의 기울기, 샘플이 모자라면 창 시작부터의 평균.

        반환: (소진 시각 또는 None, 리셋 시점 예상 %). 증가가 없으면 (None, None).
        """
        if not d["reset"]:
            return None, None
        start = (d["reset"] - BLOCK).timestamp()
        pts = [(at, p) for at, p, _ in self.samples if at >= max(start, d["at"] - 3600)]
        if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= 600:
            rate = (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0])
        elif d["at"] > start:
            rate = d["five"] / (d["at"] - start)  # 창이 0% 에서 시작했다고 본다
        else:
            return None, None
        if rate <= 0:
            return None, None
        at_reset = d["five"] + rate * (d["reset"].timestamp() - d["at"])
        if at_reset < 100:
            return None, at_reset
        return datetime.fromtimestamp(d["at"] + (100 - d["five"]) / rate).astimezone(), at_reset

    def _loop(self):
        wait = self.interval
        time.sleep(max(0.0, self.at + self.interval - time.time()))  # 캐시가 아직 새것이면 기다린다
        while True:
            status = self._fetch()
            if status == 429:
                wait = min(wait * 2, self.MAX_WAIT)
            elif status == 200:
                wait = self.interval
            time.sleep(wait)

    def _fetch(self):
        try:
            with open(self.cred, encoding="utf-8") as f:
                oauth = json.load(f)["claudeAiOauth"]
            if oauth.get("expiresAt", 0) / 1000 < time.time():
                return None  # 갱신은 Claude Code 몫
            req = urllib.request.Request(self.URL, headers={
                "Authorization": "Bearer " + oauth["accessToken"],
                "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-meter"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = json.load(r)
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return None
        self.raw, self.at = {k: raw.get(k) for k in ("five_hour", "seven_day")}, time.time()
        five, week = self.raw.get("five_hour") or {}, self.raw.get("seven_day") or {}
        self.samples = [x for x in self.samples if x[0] > self.at - BLOCK.total_seconds()]
        self.samples.append([self.at, five.get("utilization") or 0, week.get("utilization") or 0])
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"at": self.at, "raw": self.raw, "samples": self.samples}, f)
        except OSError:
            pass
        return 200


def human(n):
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(n)


class Widget:
    def __init__(self):
        self.cfg_path = os.path.join(app_dir(), "claude_meter.json")
        self.cfg = dict(DEFAULTS)
        try:
            with open(self.cfg_path, encoding="utf-8") as f:
                self.cfg.update(json.load(f))
        except (OSError, ValueError):
            pass
        root_dir = self.cfg["claude_dir"] or os.path.join(os.path.expanduser("~"), ".claude", "projects")
        self.ledger = Ledger(root_dir)
        self.plan = PlanUsage(self.cfg["plan_refresh_sec"], os.path.join(app_dir(), "claude_meter_usage.json")) if self.cfg["plan_usage"] else None
        self.tip = None

        self.root = tk.Tk()
        self.root.title(APP)
        self.root.overrideredirect(True)
        self.root.configure(bg=COLORS["bg"])
        self.root.attributes("-alpha", self.cfg["alpha"])
        self.root.attributes("-topmost", self.cfg["topmost"])
        x = self.cfg["x"] if self.cfg["x"] is not None else self.root.winfo_screenwidth() - 260
        y = self.cfg["y"] if self.cfg["y"] is not None else 80
        self.root.geometry(f"+{x}+{y}")

        font = ("Segoe UI", 9)
        self.l_ctx = tk.Label(self.root, font=("Segoe UI Semibold", 11), bg=COLORS["bg"], anchor="w")
        self.l_today = tk.Label(self.root, font=font, fg=COLORS["fg"], bg=COLORS["bg"], anchor="w")
        self.l_block = tk.Label(self.root, font=font, fg=COLORS["dim"], bg=COLORS["bg"], anchor="w")
        self.bar = tk.Canvas(self.root, height=3, width=1, bg=COLORS["bg"], highlightthickness=0)
        self.sess = tk.Frame(self.root, bg=COLORS["bg"])
        self.limits = tk.Frame(self.root, bg=COLORS["bg"])
        self.limits.columnconfigure(1, weight=1)
        self.limit_rows = []
        for r in range(2):  # 5h, 주간: [이름] [막대] [42% · 12:00]
            name = tk.Label(self.limits, font=font, fg=COLORS["dim"], bg=COLORS["bg"], anchor="w", width=4)
            meter = tk.Canvas(self.limits, height=6, width=70, bg=COLORS["bg"], highlightthickness=0)
            value = tk.Label(self.limits, font=font, fg=COLORS["fg"], bg=COLORS["bg"], anchor="w")
            name.grid(row=r, column=0, sticky="w")
            meter.grid(row=r, column=1, sticky="ew", padx=(2, 6))
            value.grid(row=r, column=2, sticky="w")
            meter.bind("<Configure>", lambda e: self.last and self._render(self.last))
            self.limit_rows.append((name, meter, value))
        self.plan_shown = False
        for w in (self.l_ctx, self.l_today, self.l_block, self.bar, self.sess, self.limits, self.root,
                  *[w for row in self.limit_rows for w in row]):
            self._bind(w)
        for w in (self.l_ctx, self.l_today, *[w for row in self.limit_rows for w in row]):
            self._hoverable(w)
        for row in self.limit_rows:  # 막대 줄은 이름·막대·값 어디에 올려도 같은 툴팁
            for w in row:
                w.tip_anchor = row[0]
        self.last = None
        self.bar.bind("<Configure>", lambda e: self.last and self._render(self.last))
        self.root.protocol("WM_DELETE_WINDOW", self._quit)  # 작업표시줄 버튼의 "창 닫기"
        if sys.platform == "win32":
            self.root.after(10, self._taskbar)
        self._layout()
        self._tick()

    def _taskbar(self):
        # 테두리 없는 창은 작업표시줄 버튼이 없어 잃어버리기 쉽다. 확장 스타일을 앱 창으로 바꿔 버튼을 만든다.
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(self.root.winfo_id())
        style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongW(hwnd, -20, (style & ~0x80) | 0x40000)  # -WS_EX_TOOLWINDOW, +WS_EX_APPWINDOW
        self.root.withdraw()  # 스타일은 창을 다시 띄울 때 반영된다
        self.root.after(10, self.root.deiconify)

    def _bind(self, w):
        w.bind("<ButtonPress-1>", self._press)
        w.bind("<B1-Motion>", self._drag)
        w.bind("<ButtonRelease-1>", lambda e: self._save())
        w.bind("<Double-Button-1>", lambda e: self._toggle("compact"))
        w.bind("<Button-3>", self._menu)

    def _hoverable(self, w):
        w.bind("<Enter>", lambda e: self._show_tip(w))
        w.bind("<Leave>", lambda e: self._hide_tip())

    def _show_tip(self, w):
        self._hide_tip()
        if not getattr(w, "tip_text", ""):
            return
        self.tip, self.tip_owner = tk.Toplevel(self.root), w
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip_label = tk.Label(self.tip, text=w.tip_text, font=("Segoe UI", 9), justify="left",
                                  bg="#2c2f37", fg=COLORS["fg"], padx=6, pady=3)
        self.tip_label.pack()
        self._place_tip()

    def _place_tip(self):
        self.tip.update_idletasks()  # 아래 줄을 가리지 않게 위젯 왼쪽 바깥, 같은 높이에 띄운다
        x = self.root.winfo_rootx() - self.tip.winfo_width() - 4
        if x < 0:
            x = self.root.winfo_rootx() + self.root.winfo_width() + 4
        anchor = getattr(self.tip_owner, "tip_anchor", self.tip_owner)
        self.tip.geometry(f"+{x}+{anchor.winfo_rooty()}")

    def _refresh_tip(self):
        # 띄워 둔 동안에도 "n분 전" 같은 값이 갱신되게 한다
        if self.tip and self.tip_owner.winfo_exists() and getattr(self.tip_owner, "tip_text", ""):
            self.tip_label.config(text=self.tip_owner.tip_text)
            self._place_tip()

    def _hide_tip(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def t(self, key):
        return TEXT[self.cfg["lang"]][key]

    def _layout(self):
        for w in (self.l_ctx, self.l_today, self.l_block, self.bar, self.sess, self.limits):
            w.pack_forget()
        self.l_ctx.pack(fill="x", padx=10, pady=(6, 0 if not self.cfg["compact"] else 6))
        if not self.cfg["compact"]:
            self.bar.pack(fill="x", padx=10, pady=(3, 2))
            self.l_today.pack(fill="x", padx=10)
            if self.plan_shown:
                self.limits.pack(fill="x", padx=10, pady=(2, 0))
            else:
                self.l_block.pack(fill="x", padx=10)
            self.sess.pack(fill="x", padx=10, pady=(2, 6))

    def _tick(self):
        try:
            self.ledger.scan()
            self._render(self.ledger.summary(self.cfg["prices"]))
        except Exception as e:  # 위젯은 죽지 않고 원인만 보여준다
            self.l_ctx.config(text=f"error: {e}"[:40], fg=COLORS["stop"])
        self._keep_on_screen()
        if self.cfg["topmost"]:
            # 테두리 없는 창은 다른 topmost 창이나 탐색기 재시작에 밀릴 수 있어 매번 다시 건다
            self.root.attributes("-topmost", False)
            self.root.attributes("-topmost", True)
        self.root.after(int(self.cfg["refresh_sec"] * 1000), self._tick)

    def _keep_on_screen(self):
        # 줄이 늘어 폭이 커져도 화면 오른쪽·아래로 잘리지 않게 안쪽으로 당긴다
        self.root.update_idletasks()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        nx = max(0, min(x, self.root.winfo_screenwidth() - self.root.winfo_width()))
        ny = max(0, min(y, self.root.winfo_screenheight() - self.root.winfo_height()))
        if (nx, ny) != (x, y):
            self.root.geometry(f"+{nx}+{ny}")

    def _render(self, s):
        self.last = s
        ctx, warn, stop = s["ctx"], self.cfg["warn_ctx"], self.cfg["stop_ctx"]
        if ctx is None:
            self.l_ctx.config(text=f"○ {self.t('none')}", fg=COLORS["dim"])
            color = COLORS["dim"]
        else:
            color = COLORS["stop"] if ctx >= stop else COLORS["warn"] if ctx >= warn else COLORS["ok"]
            extra = f"  ·  {self.t('sessions')} {s['active']}" if s["active"] > 1 else ""
            self.l_ctx.config(text=f"● {self.t('ctx')} {human(ctx)}{extra}", fg=color)
        self.bar.update_idletasks()
        w = self.bar.winfo_width()
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w, 3, fill="#2c2f37", width=0)
        if ctx:
            self.bar.create_rectangle(0, 0, w * min(ctx / stop, 1), 3, fill=color, width=0)
        self.l_today.config(text=f"{self.t('today')}  {human(s['today_tok'])}  ·  ${s['today_cost']:.2f}")
        plan = self.plan.data if self.plan else None
        if bool(plan) != self.plan_shown:  # 조회 성공/실패가 바뀌면 막대 두 줄 <-> 추정 한 줄
            self.plan_shown = bool(plan)
            self._layout()
        if plan:
            rows = [("5h", plan["five"], f"{plan['reset']:%H:%M}" if plan["reset"] else ""),
                    (self.t("week"), plan["week"], f"{plan['week_reset']:%m/%d}" if plan["week_reset"] else "")]
            for (name, meter, value), (label, pct, reset) in zip(self.limit_rows, rows):
                pct = pct or 0
                c = COLORS["stop"] if pct >= self.cfg["limit_red"] else COLORS["ok"]
                name.config(text=label)
                value.config(text=f"{pct:.0f}%" + (f" · {reset}" if reset else ""),
                             fg=COLORS["stop"] if pct >= self.cfg["limit_red"] else COLORS["fg"])
                mw = meter.winfo_width()
                meter.delete("all")
                meter.create_rectangle(0, 0, mw, 6, fill="#2c2f37", width=0)
                meter.create_rectangle(0, 0, mw * min(pct / 100, 1), 6, fill=c, width=0)
        elif s["block_left"] is None:
            self.l_block.config(text=f"{self.t('block')}  {self.t('idle')}")
        else:
            m = int(s["block_left"].total_seconds() // 60)
            self.l_block.config(text=f"{self.t('block')}  {human(s['block_tok'])}  ·  "
                                     f"{m // 60}:{m % 60:02d} {self.t('left')}")
        if not plan:
            self.l_block.config(fg=COLORS["dim"])
        self._render_sessions(s["sessions"] if s["active"] > 1 else [])
        self._render_tips(s, plan)
        self._refresh_tip()

    def _dur(self, td):
        m = max(0, int(td.total_seconds() // 60))
        if m >= 1440:
            return self.t("d").format(m // 1440, m % 1440 // 60)
        if m >= 60:
            return self.t("h").format(m // 60, m % 60)
        return self.t("m").format(m)

    def _ago(self, seconds):
        return self.t("now") if seconds < 60 else self.t("ago").format(self._dur(timedelta(seconds=seconds)))

    def _render_tips(self, s, plan):
        now = datetime.now().astimezone()
        cur = s["cur"]
        self.l_ctx.tip_text = "\n".join([
            cur["name"],
            self.t("t_cost").format(cur["cost"]),
            self.t("t_resp").format(cur["replies"], cur["tools"], self.cfg["tool_budget"]),
            self.t("t_last").format(self._ago((now - cur["last"]).total_seconds()), cur["model"]),
        ]) if cur and cur["last"] else ""

        lines = [f"{m}  ${c:.2f}" for m, c in sorted(s["models"].items(), key=lambda x: -x[1]) if c >= 0.005]
        if s["today_cost"] > 0:
            lines.append(self.t("t_sub").format(s["sub_cost"] / s["today_cost"] * 100, s["sub_cost"]))
        self.l_today.tip_text = "\n".join(lines)

        if not plan:
            return
        five_tip, week_tip = [], []
        if plan["reset"]:
            five_tip.append(self.t("t_reset").format(self._dur(plan["reset"] - now), f"{plan['reset']:%H:%M}"))
        five_tip.append(self.t("t_fetched").format(self._ago(time.time() - plan["at"])))
        eta, at_reset = self.plan.forecast(plan)
        five_tip.append(self.t("t_eta").format(
            f"{eta:%H:%M}" if eta else self.t("t_safe").format(at_reset) if at_reset is not None else self.t("t_flat")))
        if plan["week_reset"]:
            left = plan["week_reset"] - now
            week_tip.append(self.t("t_reset").format(self._dur(left), f"{plan['week_reset']:%m/%d %H:%M}"))
            frac = 1 - left / timedelta(days=7)  # 주간 창에서 지난 비율
            if plan["week"] is not None and frac > 0:
                pct, expect = plan["week"], frac * 100
                week_tip.append(self.t("t_pace").format(
                    self.t("t_ahead") if pct <= expect else self.t("t_over"), abs(expect - pct), expect))
                if frac >= 0.05:  # 창 초반엔 외삽이 크게 튄다
                    week_tip.append(self.t("t_proj").format(pct / frac))
        for (name, meter, value), tip in zip(self.limit_rows, (five_tip, week_tip)):
            for w in (name, meter, value):
                w.tip_text = "\n".join(tip)

    def _ctx_color(self, ctx, base):
        return (COLORS["stop"] if ctx >= self.cfg["stop_ctx"]
                else COLORS["warn"] if ctx >= self.cfg["warn_ctx"] else base)

    def _render_sessions(self, sessions):
        # 세션#1, 세션#2 (최근 응답 순) + 3개 이상이면 나머지는 "기타 N개". 이름은 마우스를 올렸을 때만.
        rows = []
        for i, (_, ctx, name) in enumerate(sessions[:2], 1):
            rows.append((f"▸ {self.t('session')}#{i}  {human(ctx)}", self._ctx_color(ctx, COLORS["dim"]), name))
        rest = sessions[2:]
        if rest:
            top = max(c for _, c, _ in rest)
            rows.append((f"▸ {self.t('others')} {len(rest)}{self.t('count')}  ≤{human(top)}",
                         self._ctx_color(top, COLORS["dim"]),
                         "\n".join(f"{n}  {human(c)}" for _, c, n in rest)))
        labels = self.sess.winfo_children()
        for i, (text, color, tip) in enumerate(rows):
            if i < len(labels):
                lbl = labels[i]
            else:
                lbl = tk.Label(self.sess, font=("Segoe UI", 9), bg=COLORS["bg"], anchor="w")
                lbl.pack(fill="x")
                self._bind(lbl)
                lbl.bind("<Enter>", lambda e, w=lbl: self._show_tip(w))
                lbl.bind("<Leave>", lambda e: self._hide_tip())
            lbl.config(text=text, fg=color)
            lbl.tip_text = tip
        for lbl in labels[len(rows):]:
            lbl.destroy()

    def _press(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _toggle(self, key):
        self.cfg[key] = not self.cfg[key]
        self.root.attributes("-topmost", self.cfg["topmost"])
        self._layout()
        self._save()

    def _set(self, key, value):
        self.cfg[key] = value
        self.root.attributes("-alpha", self.cfg["alpha"])
        self._save()
        self._tick_once()

    def _tick_once(self):
        self._render(self.ledger.summary(self.cfg["prices"]))

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=self.t("refresh"), command=self._tick_once)
        m.add_checkbutton(label=self.t("top"), onvalue=True, offvalue=False,
                          variable=tk.BooleanVar(value=self.cfg["topmost"]),
                          command=lambda: self._toggle("topmost"))
        sub = tk.Menu(m, tearoff=0)
        for a in (1.0, 0.88, 0.7, 0.5):
            sub.add_command(label=f"{int(a * 100)}%", command=lambda a=a: self._set("alpha", a))
        m.add_cascade(label=self.t("opacity"), menu=sub)
        m.add_command(label=self.t("lang"),
                      command=lambda: self._set("lang", "en" if self.cfg["lang"] == "ko" else "ko"))
        m.add_separator()
        m.add_command(label=self.t("quit"), command=self._quit)
        m.tk_popup(e.x_root, e.y_root)

    def _save(self):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass  # 읽기 전용 위치에서 실행해도 동작은 한다

    def _quit(self):
        self._save()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 고해상도 화면에서 글자 번짐 방지
        except Exception:
            pass
    Widget().run()
