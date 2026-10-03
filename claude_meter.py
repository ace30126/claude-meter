"""Claude Meter - Claude Code 토큰 사용량을 화면 위에 띄워 두는 작은 위젯.

데이터: ~/.claude/projects/**/*.jsonl (Claude Code 가 응답마다 남기는 usage).
네트워크·API 키 없이 로컬 로그만 읽는다. claude.ai 웹/데스크톱 채팅은 로그가 없어 잡히지 않는다.

표시
  1. 현재 세션 컨텍스트 (마지막 메인 응답의 input + cache_creation + cache_read)
  2. 오늘 누적 토큰 · API 단가 환산 비용
  3. 플랜 한도 사용률(5시간·주간). 실패하면 로그로 추정한 5시간 블록 토큰 · 남은 시간
  4. 활성 세션이 2개 이상이면 세션#1, 세션#2, 기타 N개 (마우스를 올리면 작업 폴더 이름)

한도 사용률은 Claude Code 로그인 토큰(~/.claude/.credentials.json)으로 /usage 와 같은 비공개 엔드포인트를
120초마다 읽는다(429 시 백오프, 응답은 claude_meter_usage.json 에 캐시). 토큰을 갱신하지는 않는다(Claude Code 로그인이 꼬일 수 있음). 만료되면 Claude Code 가 갱신할 때까지 추정값을 쓴다.

조작: 왼쪽 드래그 = 이동, 더블클릭 = 접기/펴기, 오른쪽 클릭 = 메뉴.
"""
import glob
import json
import os
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
}
TEXT = {
    "ko": {"ctx": "컨텍스트", "today": "오늘", "block": "5h 블록", "left": "남음", "idle": "블록 없음",
           "limit": "한도", "reset": "리셋", "week": "주간", "session": "세션", "others": "기타", "count": "개",
           "sessions": "활성", "refresh": "새로고침", "top": "항상 위", "opacity": "투명도",
           "lang": "English", "quit": "종료", "none": "세션 없음"},
    "en": {"ctx": "Context", "today": "Today", "block": "5h block", "left": "left", "idle": "no block",
           "limit": "Limit", "reset": "reset", "week": "week", "session": "Session", "others": "Others", "count": "",
           "sessions": "active", "refresh": "Refresh", "top": "Always on top", "opacity": "Opacity",
           "lang": "한국어", "quit": "Quit", "none": "no session"},
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
        self.events = {}    # message id -> (datetime, model, usage)

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
            self.events[mid] = (ts, model, u)
            if not is_sub and not d.get("isSidechain"):
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

        today_tok = sum(total_tokens(u) for ts, _, u in evs if ts >= midnight)
        today_cost = sum(cost_of(m, u, overrides) for ts, m, u in evs if ts >= midnight)

        # 5시간 블록: 첫 메시지 시각을 정시로 내림한 지점부터 5시간. 그 뒤 첫 메시지가 새 블록을 연다.
        block_start, block_tok = None, 0
        for ts, _, u in evs:
            if block_start is None or ts >= block_start + BLOCK:
                block_start, block_tok = ts.replace(minute=0, second=0, microsecond=0), 0
            block_tok += total_tokens(u)
        if block_start is None or now >= block_start + BLOCK:
            block_left, block_tok = None, 0
        else:
            block_left = block_start + BLOCK - now

        live = sorted(((ts, c, f"{session_name(self.cwd.get(p))}  ({self.start[p].astimezone():%H:%M}~)") for p, (ts, c) in self.ctx.items()
                       if now - ts < timedelta(minutes=10)), reverse=True)
        latest = max(self.ctx.values(), default=None)
        return {"ctx": latest[1] if latest else None, "active": len(live), "sessions": live,
                "today_tok": today_tok, "today_cost": today_cost,
                "block_tok": block_tok, "block_left": block_left}


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
        self.raw, self.at = None, 0.0
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            self.raw, self.at = c["raw"], c["at"]
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
        return {"five": five["utilization"], "week": week.get("utilization"),
                "reset": to_local(five.get("resets_at")), "week_reset": to_local(week.get("resets_at"))}

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
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"at": self.at, "raw": self.raw}, f)
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
        self.last = None
        self.bar.bind("<Configure>", lambda e: self.last and self._render(self.last))
        self._layout()
        self._tick()

    def _bind(self, w):
        w.bind("<ButtonPress-1>", self._press)
        w.bind("<B1-Motion>", self._drag)
        w.bind("<ButtonRelease-1>", lambda e: self._save())
        w.bind("<Double-Button-1>", lambda e: self._toggle("compact"))
        w.bind("<Button-3>", self._menu)

    def _show_tip(self, w):
        self._hide_tip()
        if not getattr(w, "tip_text", ""):
            return
        self.tip = tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=w.tip_text, font=("Segoe UI", 9), justify="left",
                 bg="#2c2f37", fg=COLORS["fg"], padx=6, pady=3).pack()
        self.tip.update_idletasks()  # 아래 줄을 가리지 않게 위젯 왼쪽 바깥, 같은 높이에 띄운다
        x = self.root.winfo_rootx() - self.tip.winfo_width() - 4
        if x < 0:
            x = self.root.winfo_rootx() + self.root.winfo_width() + 4
        self.tip.geometry(f"+{x}+{w.winfo_rooty()}")

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
