"""
Ichimoku crossover scanner + mailer - NSE stocks with market cap above Rs 5,000 crore.

  Daily chart : Conversion 18 / Base 52   (Span B 104, displacement 52)
  Weekly chart: Conversion 9  / Base 26   (Span B 52,  displacement 26)

Storage (mailer history, "already sent today" flag, run lock) lives in Upstash Redis
when KV_REST_API_URL / KV_REST_API_TOKEN (or UPSTASH_REDIS_REST_URL / _TOKEN) are set,
otherwise in a local JSON file (for running on your own computer).
"""
import json
import os
import smtplib
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

os.environ.setdefault("XDG_CACHE_HOME", "/tmp")  # serverless disk is read-only except /tmp

import numpy as np
import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))
MCAP_MIN_INR = 5_000 * 1e7          # Rs 5,000 crore
DAILY = dict(conv=18, base=52)
WEEKLY = dict(conv=9, base=26)
LOOKBACK = 5                        # trading sessions
HISTORY_KEEP = 10                   # mailers remembered for the drop-out check
TIME_BUDGET = int(os.getenv("TIME_BUDGET_SECONDS", "220"))  # stay inside Vercel's 300 s limit

K_HISTORY, K_SENT, K_LOCK = "ichimoku:history", "ichimoku:last_sent", "ichimoku:lock"


# ------------------------------------------------------------- storage ----
class Store:
    def __init__(self):
        self.url = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
        self.token = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")
        self.file = Path(os.getenv("STATE_FILE", "state.json"))

    @property
    def remote(self):
        return bool(self.url and self.token)

    def _cmd(self, *args):
        r = requests.post(self.url, json=[str(a) for a in args], timeout=15,
                          headers={"Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        return r.json().get("result")

    def _load(self):
        return json.loads(self.file.read_text()) if self.file.exists() else {}

    def get(self, key):
        return self._cmd("GET", key) if self.remote else self._load().get(key)

    def set(self, key, value, ex=None, nx=False):
        if self.remote:
            args = ["SET", key, value] + (["NX"] if nx else []) + (["EX", ex] if ex else [])
            return self._cmd(*args)
        data = self._load()
        if nx and key in data:
            return None
        data[key] = value
        self.file.write_text(json.dumps(data))
        return "OK"

    def delete(self, key):
        if self.remote:
            return self._cmd("DEL", key)
        data = self._load()
        data.pop(key, None)
        self.file.write_text(json.dumps(data))


# ---------------------------------------------------------------- data ----
def get_universe() -> pd.DataFrame:
    """All NSE stocks above the market-cap cutoff, from TradingView's public screener."""
    payload = {
        "filter": [
            {"left": "market_cap_basic", "operation": "greater", "right": MCAP_MIN_INR},
            {"left": "exchange", "operation": "equal", "right": "NSE"},
            {"left": "type", "operation": "equal", "right": "stock"},
        ],
        "columns": ["name", "description", "market_cap_basic"],
        "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"},
        "range": [0, 3000],
    }
    r = requests.post("https://scanner.tradingview.com/india/scan", json=payload,
                      headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    rows = []
    for item in r.json()["data"]:
        ticker, name, mcap = item["d"]
        rows.append({"symbol": ticker, "name": name, "mcap_cr": (mcap or 0) / 1e7,
                     "yf": ticker.replace("_", "-") + ".NS"})
    return pd.DataFrame(rows)


def download(tickers, deadline):
    """2 years of daily OHLC per ticker from Yahoo Finance, in batches, until the deadline."""
    import yfinance as yf
    try:
        yf.set_tz_cache_location("/tmp/yf-cache")
    except Exception:
        pass
    out = {}
    for i in range(0, len(tickers), 100):
        if time.time() > deadline:
            print(f"Time budget reached after {i} tickers")
            break
        batch = tickers[i:i + 100]
        data = None
        for attempt in range(2):
            try:
                data = yf.download(batch, period="2y", interval="1d", group_by="ticker",
                                   auto_adjust=False, threads=True, progress=False)
                break
            except Exception as e:
                print(f"batch {i}: retry {attempt + 1} ({e})")
                time.sleep(3)
        if data is None or data.empty:
            continue
        for t in batch:
            try:
                df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            except KeyError:
                continue
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["High", "Low", "Close"])
            if len(df) >= 160:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                out[t] = df
    return out


# ------------------------------------------------------------ ichimoku ----
def ichimoku(df, conv, base):
    """Matches TradingView: Span B length = 2 x base, cloud shifted forward (base - 1) bars."""
    h, l = df["High"], df["Low"]
    mid = lambda n: (h.rolling(n).max() + l.rolling(n).min()) / 2
    cl, bl = mid(conv), mid(base)
    return pd.DataFrame({"cl": cl, "bl": bl,
                         "span_a": ((cl + bl) / 2).shift(base - 1),
                         "span_b": mid(2 * base).shift(base - 1)})


def line_state(ich):
    """+1 when conversion is above base, -1 when below; ties keep the previous side."""
    return np.sign(ich["cl"] - ich["bl"]).replace(0, np.nan).ffill()


def to_weekly(df):
    return (df.resample("W-FRI")
              .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
              .dropna(subset=["Close"]))


def daily_events(df):
    ich = ichimoku(df, **DAILY)
    s = line_state(ich)
    crossed = s.ne(s.shift()) & s.notna() & s.shift().notna()
    recent = crossed.iloc[-LOOKBACK:]
    return ich.iloc[-1], [(d, "Bullish" if s.loc[d] > 0 else "Bearish") for d in recent[recent].index]


def _last_state(hs, ls, conv, base):
    """line_state() of the final bar, computed with plain numpy on a short window."""
    from numpy.lib.stride_tricks import sliding_window_view as win
    if len(hs) < base:
        return np.nan
    cl = (win(hs[base - conv:], conv).max(1) + win(ls[base - conv:], conv).min(1)) / 2
    bl = (win(hs, base).max(1) + win(ls, base).min(1)) / 2
    signs = np.sign(cl - bl)
    signs = signs[signs != 0]
    return float(signs[-1]) if len(signs) else np.nan


def weekly_events(df):
    """Rebuild the weekly chart as it looked on each of the last sessions, so a cross
    is dated to the day it first showed on the weekly chart."""
    wk = to_weekly(df)
    last = ichimoku(wk, **WEEKLY).iloc[-1]              # full history for the cloud
    d_idx, H, L = df.index.values, df["High"].values, df["Low"].values
    w_idx, wH, wL = wk.index.values, wk["High"].values, wk["Low"].values
    n = len(df)
    dates = df.index[-(LOOKBACK + 1):]
    week_of = dates.to_period("W-FRI").end_time.normalize().values
    states = []
    for j, wend in enumerate(week_of):
        p = n - len(dates) + j                          # position of this session in df
        k = np.searchsorted(w_idx, wend, "left")        # completed weeks before this week
        start = np.searchsorted(d_idx, w_idx[k - 1], "right") if k else 0
        hs = np.append(wH[max(0, k - 40):k], H[start:p + 1].max())
        ls = np.append(wL[max(0, k - 40):k], L[start:p + 1].min())
        states.append(_last_state(hs, ls, **WEEKLY))
    events = [(dates[i], "Bullish" if states[i] > 0 else "Bearish")
              for i in range(1, len(dates))
              if pd.notna(states[i]) and pd.notna(states[i - 1]) and states[i] != states[i - 1]]
    return last, events


def cloud_position(close, a, b):
    if pd.isna(a) or pd.isna(b):
        return "-"
    top, bottom = max(a, b), min(a, b)
    return "Above cloud" if close > top else "Below cloud" if close < bottom else "Inside cloud"


def scan(universe, prices):
    as_of = pd.Series([df.index[-1] for df in prices.values()]).mode().iloc[0]
    lists = {"daily_today": [], "daily_5d": [], "weekly_today": [], "weekly_5d": []}
    for _, u in universe.iterrows():
        df = prices.get(u["yf"])
        if df is None or df.index[-1] != as_of:
            continue
        close = df["Close"].iloc[-1]
        base_row = {"Stock": u["name"], "Symbol": u["symbol"], "Close": close,
                    "Chg 1D %": (close / df["Close"].iloc[-2] - 1) * 100,
                    "Chg 1W %": (close / df["Close"].iloc[-6] - 1) * 100}
        for tf, fn in (("daily", daily_events), ("weekly", weekly_events)):
            ich, events = fn(df)
            if not events:
                continue
            when, direction = events[-1]
            row = dict(base_row, **{
                "Signal": direction, "Cross date": when.strftime("%d-%b"),
                "Conversion": ich["cl"], "Base": ich["bl"],
                "Span A": ich["span_a"], "Span B": ich["span_b"],
                "Cloud": cloud_position(close, ich["span_a"], ich["span_b"])})
            lists[f"{tf}_5d"].append(row)
            if when == as_of:
                lists[f"{tf}_today"].append(row)
    return lists, as_of


# ------------------------------------------------------------- history ----
def load_history(store):
    raw = store.get(K_HISTORY)
    return json.loads(raw) if raw else []


def save_history(store, history, lists, as_of):
    names = {r["Symbol"]: r["Stock"] for k in lists for r in lists[k]}
    history = [m for m in history if m["date"] != as_of.strftime("%Y-%m-%d")]
    history.append({"date": as_of.strftime("%Y-%m-%d"),
                    "daily": sorted({r["Symbol"] for r in lists["daily_5d"]}),
                    "weekly": sorted({r["Symbol"] for r in lists["weekly_5d"]}),
                    "names": names})
    store.set(K_HISTORY, json.dumps(history[-HISTORY_KEEP:]))


def dropped_out(history, lists, tf):
    current = {r["Symbol"] for r in lists[f"{tf}_5d"]}
    seen, names = {}, {}
    for m in history[-HISTORY_KEEP:]:
        names.update(m.get("names", {}))
        for sym in m.get(tf, []):
            first, last = seen.get(sym, (m["date"], m["date"]))
            seen[sym] = (min(first, m["date"]), max(last, m["date"]))
    rows = [{"Stock": names.get(s, s), "Symbol": s, "first": f, "last": l}
            for s, (f, l) in seen.items() if s not in current]
    return sorted(rows, key=lambda r: r["first"])


# ---------------------------------------------------------------- html ----
CSS = """
body{font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#222}
h2{margin:22px 0 6px;font-size:16px;border-bottom:2px solid #1f3b64;padding-bottom:3px;color:#1f3b64}
table{border-collapse:collapse;width:100%;margin-bottom:8px}
th{background:#1f3b64;color:#fff;padding:6px;text-align:left;font-weight:600;white-space:nowrap}
td{padding:5px 6px;border-bottom:1px solid #e3e3e3;white-space:nowrap}
tr:nth-child(even) td{background:#f7f9fc}
.bull{color:#0a7d32;font-weight:700}.bear{color:#c0262d;font-weight:700}
.pos{color:#0a7d32}.neg{color:#c0262d}
.new{background:#1f3b64;color:#fff;border-radius:3px;padding:1px 4px;font-size:10px;margin-left:4px}
.dropped td{background:#fff4cc !important}
.warn{background:#fde2e1;border:1px solid #c0262d;padding:8px;margin:8px 0}
.note{color:#666;font-size:11px}
"""


def fmt(v, pct=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    if pct:
        cls = "pos" if v > 0 else "neg" if v < 0 else ""
        return f'<span class="{cls}">{v:+.2f}%</span>'
    return f"{v:,.2f}"


def crossover_table(rows, seen_before):
    if not rows:
        return '<p class="note">No crossovers.</p>'
    rows = sorted(rows, key=lambda r: (r["Signal"] != "Bullish", r["Symbol"]))
    head = ("<tr><th>#</th><th>Stock</th><th>Symbol</th><th>Close</th><th>1D %</th><th>1W %</th>"
            "<th>Signal</th><th>Cross date</th><th>Conversion</th><th>Base</th>"
            "<th>Span A</th><th>Span B</th><th>Price vs cloud</th></tr>")
    body = []
    for i, r in enumerate(rows, 1):
        tag = "" if r["Symbol"] in seen_before else '<span class="new">NEW</span>'
        sig = "bull" if r["Signal"] == "Bullish" else "bear"
        body.append(
            f"<tr><td>{i}</td><td>{r['Stock']}{tag}</td><td>{r['Symbol']}</td><td>{fmt(r['Close'])}</td>"
            f"<td>{fmt(r['Chg 1D %'], True)}</td><td>{fmt(r['Chg 1W %'], True)}</td>"
            f"<td class='{sig}'>{r['Signal']}</td><td>{r['Cross date']}</td>"
            f"<td>{fmt(r['Conversion'])}</td><td>{fmt(r['Base'])}</td>"
            f"<td>{fmt(r['Span A'])}</td><td>{fmt(r['Span B'])}</td><td>{r['Cloud']}</td></tr>")
    return f"<table>{head}{''.join(body)}</table>"


def dropped_table(rows):
    if not rows:
        return '<p class="note">None - no stock from the previous 10 mailers has dropped out.</p>'
    d = lambda s: pd.Timestamp(s).strftime("%d-%b-%Y")
    head = "<tr><th>Stock</th><th>Symbol</th><th>First included</th><th>Last seen</th></tr>"
    body = "".join(f"<tr class='dropped'><td>{r['Stock']}</td><td>{r['Symbol']}</td>"
                   f"<td>{d(r['first'])}</td><td>{d(r['last'])}</td></tr>" for r in rows)
    return f"<table>{head}{body}</table>"


def build_html(lists, as_of, n_scanned, n_universe, history):
    seen = {s for m in history[-HISTORY_KEEP:] for tf in ("daily", "weekly") for s in m.get(tf, [])}
    warn = ""
    if n_scanned < 0.9 * n_universe:
        warn = (f"<div class='warn'>Only {n_scanned} of {n_universe} stocks could be scanned today "
                f"(data source slow or unavailable). Lists below may be incomplete.</div>")
    sections = [
        ("1. Daily chart (18 / 52) - crossovers today", crossover_table(lists["daily_today"], seen)),
        (f"2. Daily chart (18 / 52) - crossovers in last {LOOKBACK} sessions",
         crossover_table(lists["daily_5d"], seen)),
        ("3. Weekly chart (9 / 26) - crossovers today", crossover_table(lists["weekly_today"], seen)),
        (f"4. Weekly chart (9 / 26) - crossovers in last {LOOKBACK} sessions",
         crossover_table(lists["weekly_5d"], seen)),
        ("5. Dropped out - daily list (was in previous 10 mailers)",
         dropped_table(dropped_out(history, lists, "daily"))),
        ("6. Dropped out - weekly list (was in previous 10 mailers)",
         dropped_table(dropped_out(history, lists, "weekly"))),
    ]
    parts = "".join(f"<h2>{t}</h2>{tbl}" for t, tbl in sections)
    return (f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
            f"<p><b>Ichimoku crossover scan - {as_of:%d %b %Y}</b><br>"
            f"Scanned {n_scanned} of {n_universe} NSE stocks with market cap above Rs 5,000 cr. "
            f"Crossover = Conversion line crossing the Base line (Bullish = crossing above).</p>"
            f"{warn}{parts}"
            f"<p class='note'>Conversion/Base/Span values are for the chart named in each section. "
            f"NEW = not in any of the previous 10 mailers. 1W % = change over the last 5 sessions. "
            f"Automated scan for information only, not investment advice.</p></body></html>")


# ---------------------------------------------------------------- mail ----
def send_mail(html, subject):
    user, pwd, to = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"], os.environ["MAIL_TO"]
    host, port = os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


# ----------------------------------------------------------------- run ----
def run(send=True, force=False):
    """Returns (http_status, body, content_type).
    send=False -> build the report and return it as HTML (preview), nothing saved.
    force=True -> ignore the holiday check and the 'already sent today' guard."""
    t0 = time.time()
    store = Store()
    today = datetime.now(IST).date().isoformat()

    if send and not force and store.get(K_SENT) == today:
        return 200, f"Mail for {today} already sent - nothing to do.", "text/plain"
    if store.set(K_LOCK, "1", ex=330, nx=True) is None:
        return 409, "Another scan is running right now - try again in a few minutes.", "text/plain"

    try:
        universe = get_universe()
        prices = download(universe["yf"].tolist(), deadline=t0 + TIME_BUDGET)
        if not prices:
            raise RuntimeError("No price data received from Yahoo Finance.")
        lists, as_of = scan(universe, prices)
        if as_of.date().isoformat() != today and not force:
            return 200, f"Latest market data is {as_of.date()} - market holiday? No mail sent.", "text/plain"

        history = [m for m in load_history(store) if m["date"] != as_of.strftime("%Y-%m-%d")]
        html = build_html(lists, as_of, len(prices), len(universe), history)
        if not send:
            return 200, html, "text/html; charset=utf-8"

        subject = (f"Ichimoku scan {as_of:%d-%b-%Y} | Daily: {len(lists['daily_today'])} today, "
                   f"{len(lists['daily_5d'])} in 5d | Weekly: {len(lists['weekly_today'])} today, "
                   f"{len(lists['weekly_5d'])} in 5d")
        send_mail(html, subject)
        save_history(store, history, lists, as_of)
        store.set(K_SENT, today)
        return 200, f"Sent in {time.time() - t0:.0f}s: {subject}", "text/plain"

    except Exception:
        tb = traceback.format_exc()
        print(tb)
        if send:
            try:
                send_mail(f"<pre>{tb}</pre>", f"Ichimoku scan FAILED {today}")
            except Exception:
                pass
        return 500, tb, "text/plain"
    finally:
        store.delete(K_LOCK)
