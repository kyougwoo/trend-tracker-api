"""
TREND TRACKER Flask API — pykrx 실시간 연동 버전
Render.com 무료 배포용 (계좌 불필요)

수정 이력:
  - pykrx import를 함수 안으로 이동 (CRITICAL: 모듈 레벨 크래시 방지)
  - idx.strftime() 사용 (CRITICAL: 날짜 파싱 안전하게)
  - /api/warmup 엔드포인트 추가 (HIGH: 콜드스타트 타임아웃 방지)
  - /api/health에 source 필드 추가 (HIGH: 프론트 상태 표시 정확하게)
  - 샘플 날짜 date.today() 기준으로 수정 (CRITICAL: 2025 고정값 제거)
  - Flask-SocketIO 실시간 WebSocket 연결 추가 (종목 구독/실시간 업데이트)
"""

import os
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from flask import Flask, jsonify, request, Response
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        r = Response(status=200)
        r.headers["Access-Control-Allow-Origin"]  = "*"
        r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return r

def _check_pykrx():
    try:
        from pykrx import stock
        print("  pykrx 로드 성공")
        return True
    except Exception as e:
        print("  pykrx 없음:", e)
        return False

PYKRX_AVAILABLE = _check_pykrx()
DATA_SOURCE = "pykrx" if PYKRX_AVAILABLE else "sample"
print("  데이터 소스:", DATA_SOURCE)

DEFAULT_STOCKS = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "051910": "LG화학",
    "005380": "현대차",
    "068270": "셀트리온",
    "035720": "카카오",
    "207940": "삼성바이오로직스",
}

_cache      = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 3600

def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        if (datetime.now() - item["ts"]).total_seconds() > CACHE_TTL:
            del _cache[key]
            return None
        return item["data"]

def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": datetime.now()}

def fetch_ohlcv_pykrx(ticker, months=36):
    from pykrx import stock as krx
    from dateutil.relativedelta import relativedelta
    end   = date.today()
    start = end - relativedelta(months=months + 2)
    df = krx.get_market_ohlcv(
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
        ticker,
        freq="m"
    )
    if df is None or len(df) == 0:
        raise ValueError("데이터 없음: " + ticker)
    rows = []
    for idx, row in df.iterrows():
        try:
            label = idx.strftime("%Y-%m")
        except AttributeError:
            label = str(idx)[:7]
        rows.append({
            "date":   label,
            "open":   int(row.get("시가",   row.get("Open",   0))),
            "high":   int(row.get("고가",   row.get("High",   0))),
            "low":    int(row.get("저가",   row.get("Low",    0))),
            "close":  int(row.get("종가",   row.get("Close",  0))),
            "volume": int(row.get("거래량", row.get("Volume", 0))),
        })
    seen = {}
    for r in rows:
        if r["close"] > 0:
            seen[r["date"]] = r
    return sorted(seen.values(), key=lambda x: x["date"])[-months:]

def fetch_ohlcv_sample(ticker, months=36):
    random.seed(int(ticker) % 99991)
    price = 20000 + random.random() * 60000
    today = date.today()
    rows  = []
    for i in range(months, 0, -1):
        yr = today.year  + (today.month - i - 1) // 12
        mo = (today.month - i - 1) % 12 + 1
        lbl = "%d-%02d" % (yr, mo)
        ret = 0.007 + (random.random() - 0.5) * 0.10
        o   = round(price * (1 + (random.random()-0.5)*0.02) / 100) * 100
        c   = round(o * (1 + ret) / 100) * 100
        rows.append({
            "date":   lbl,
            "open":   max(o, 100),
            "high":   round(max(o,c)*(1+random.random()*0.025)/100)*100,
            "low":    round(min(o,c)*(1-random.random()*0.025)/100)*100,
            "close":  max(c, 100),
            "volume": random.randint(500000, 3000000),
        })
        price = max(c, 100)
    return rows

def fetch_ohlcv(ticker, months=36):
    if PYKRX_AVAILABLE:
        try:
            rows = fetch_ohlcv_pykrx(ticker, months)
            print("  [pykrx] %s — %d rows" % (ticker, len(rows)))
            return rows
        except Exception as e:
            print("  [pykrx ERROR] %s: %s" % (ticker, str(e)[:80]))
    print("  [SAMPLE] " + ticker)
    return fetch_ohlcv_sample(ticker, months)

def rolling_mean(arr, n, i):
    return sum(arr[i-n+1:i+1]) / n if i >= n - 1 else None

def analyze(rows):
    closes = [r["close"] for r in rows]
    avgvol = sum(r["volume"] for r in rows) / max(len(rows), 1)
    for i, row in enumerate(rows):
        row["ma5"]  = round(rolling_mean(closes, 5,  i)) if i >= 4  else None
        row["ma10"] = round(rolling_mean(closes, 10, i)) if i >= 9  else None
        row["ma20"] = round(rolling_mean(closes, 20, i)) if i >= 19 else None
        row["volStrong"] = row["volume"] > avgvol * 1.2
        ma5, ma10, ma20 = row["ma5"], row["ma10"], row["ma20"]
        if not (ma5 and ma10 and ma20):
            row["alignment"] = "--"
            row["forking"]   = False
            row["signal"]    = "--"
            continue
        if   ma5 > ma10 > ma20: row["alignment"] = "정배열"
        elif ma5 < ma10 < ma20: row["alignment"] = "역배열"
        else:                   row["alignment"] = "혼조"
        row["forking"] = False
        if i > 0:
            p = rows[i-1]
            pm5, pm10 = p.get("ma5"), p.get("ma10")
            if pm5 and pm10:
                row["forking"] = abs(ma5-ma10) > abs(pm5-pm10) and ma5 > ma10 > ma20
        c     = row["close"]
        volOk = row["volStrong"]
        if   c > ma5 and ma5 > ma10 > ma20 and row["forking"] and volOk: row["signal"] = "BUY"
        elif c < ma5 and ma5 < ma10 < ma20:                              row["signal"] = "SELL"
        elif ma5 > ma10 > ma20:                                           row["signal"] = "HOLD"
        else:                                                             row["signal"] = "WAIT"
    return rows

def get_stock_data(ticker):
    cached = cache_get(ticker)
    if cached:
        return cached
    rows = fetch_ohlcv(ticker, 36)
    rows = analyze(rows)
    cache_set(ticker, rows)
    return rows

@app.route("/api/stocks")
def api_stocks():
    def _fetch(item):
        ticker, name = item
        rows   = get_stock_data(ticker)
        latest = rows[-1]
        prev   = rows[-2] if len(rows) >= 2 else latest
        return {
            "ticker":     ticker,
            "name":       name,
            "date":       latest["date"],
            "close":      latest["close"],
            "prev":       prev["close"],
            "change":     latest["close"] - prev["close"],
            "change_pct": round((latest["close"]-prev["close"]) / max(prev["close"],1) * 100, 2),
            "ma5":        latest["ma5"],
            "ma10":       latest["ma10"],
            "ma20":       latest["ma20"],
            "alignment":  latest["alignment"],
            "forking":    latest["forking"],
            "signal":     latest["signal"],
            "volStrong":  latest.get("volStrong", False),
            "source":     DATA_SOURCE,
        }
    result = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_fetch, item): item for item in DEFAULT_STOCKS.items()}
        for fut in as_completed(futures):
            try:
                result.append(fut.result())
            except Exception as e:
                print("  [ERROR]", e)
    priority = {"BUY":0,"SELL":1,"HOLD":2,"WAIT":3,"--":4}
    result.sort(key=lambda x: priority.get(x["signal"], 9))
    return jsonify({
        "ok":      True,
        "data":    result,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source":  DATA_SOURCE,
    })

@app.route("/api/stock/<ticker>")
def api_stock(ticker):
    if ticker not in DEFAULT_STOCKS:
        return jsonify({"ok": False, "error": "종목 없음"}), 404
    rows = get_stock_data(ticker)
    return jsonify({
        "ok":     True,
        "ticker": ticker,
        "name":   DEFAULT_STOCKS[ticker],
        "data":   rows,
        "source": DATA_SOURCE,
    })

@app.route("/api/scan")
def api_scan():
    result = []
    for ticker, name in DEFAULT_STOCKS.items():
        rows   = get_stock_data(ticker)
        latest = rows[-1]
        result.append({
            "ticker":    ticker,
            "name":      name,
            "signal":    latest["signal"],
            "close":     latest["close"],
            "date":      latest["date"],
            "alignment": latest.get("alignment", "--"),
            "forking":   latest.get("forking", False),
        })
    priority = {"BUY":0,"SELL":1,"HOLD":2,"WAIT":3,"--":4}
    result.sort(key=lambda x: priority.get(x["signal"], 9))
    return jsonify({"ok": True, "data": result, "source": DATA_SOURCE,
                    "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    with _cache_lock:
        _cache.clear()
    for ticker in DEFAULT_STOCKS:
        get_stock_data(ticker)
    return jsonify({"ok": True, "source": DATA_SOURCE,
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

@app.route("/api/warmup")
def api_warmup():
    status = "pykrx ready" if PYKRX_AVAILABLE else "sample mode"
    return jsonify({"ok": True, "status": status, "source": DATA_SOURCE})

@app.route("/api/ai-comment", methods=["POST"])
def api_ai_comment():
    import requests as req
    body   = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "")
    if not prompt:
        return jsonify({"ok": False, "error": "prompt 필드 필요"}), 400
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY 없음"}), 503
    try:
        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json",
                     "x-api-key": anthropic_key,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        data = res.json()
        text = data.get("content", [{}])[0].get("text", "분석 결과 없음")
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/health")
def api_health():
    return jsonify({
        "ok":     True,
        "time":   datetime.now().isoformat(),
        "source": DATA_SOURCE,
        "pykrx":  PYKRX_AVAILABLE,
        "cached": len(_cache),
    })

@app.route("/")
def root():
    return jsonify({"service": "TREND TRACKER API", "version": "4.0",
                    "source": DATA_SOURCE, "pykrx": PYKRX_AVAILABLE,
                    "realtime": True})


# ── WebSocket 실시간 연결 ─────────────────────────────────────────

# 구독 관리: { sid: set(ticker, ...) }
_subscriptions = {}
_subs_lock = threading.Lock()

# 마지막으로 브로드캐스트한 시그널 저장 (변경 감지용)
_last_signals = {}

# 실시간 업데이트 주기 (초)
REALTIME_INTERVAL = int(os.environ.get("REALTIME_INTERVAL", "60"))


def _build_stock_summary(ticker, name, rows):
    """종목 데이터를 클라이언트 전송용 요약 dict로 변환"""
    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else latest
    return {
        "ticker":     ticker,
        "name":       name,
        "date":       latest["date"],
        "close":      latest["close"],
        "prev":       prev["close"],
        "change":     latest["close"] - prev["close"],
        "change_pct": round((latest["close"] - prev["close"]) / max(prev["close"], 1) * 100, 2),
        "ma5":        latest["ma5"],
        "ma10":       latest["ma10"],
        "ma20":       latest["ma20"],
        "alignment":  latest["alignment"],
        "forking":    latest["forking"],
        "signal":     latest["signal"],
        "volStrong":  latest.get("volStrong", False),
        "source":     DATA_SOURCE,
        "updated":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@socketio.on("connect")
def handle_connect():
    sid = request.sid
    with _subs_lock:
        _subscriptions[sid] = set()
    print("  [WS] 연결: %s" % sid)
    emit("connected", {"sid": sid, "message": "실시간 연결 성공"})


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    with _subs_lock:
        _subscriptions.pop(sid, None)
    print("  [WS] 해제: %s" % sid)


@socketio.on("subscribe")
def handle_subscribe(data):
    """종목 구독. data: { "tickers": ["005930", ...] } 또는 { "ticker": "005930" }"""
    sid = request.sid
    tickers = data.get("tickers") or []
    if not tickers and data.get("ticker"):
        tickers = [data["ticker"]]

    added = []
    for t in tickers:
        if t in DEFAULT_STOCKS:
            with _subs_lock:
                _subscriptions.setdefault(sid, set()).add(t)
            join_room("stock:" + t)
            added.append(t)

    print("  [WS] 구독 (%s): %s" % (sid[:8], added))
    emit("subscribed", {"tickers": added})

    # 구독 즉시 최신 데이터 전송
    for t in added:
        try:
            rows = get_stock_data(t)
            summary = _build_stock_summary(t, DEFAULT_STOCKS[t], rows)
            emit("stock_update", summary)
        except Exception as e:
            emit("error", {"ticker": t, "message": str(e)})


@socketio.on("unsubscribe")
def handle_unsubscribe(data):
    """종목 구독 해제. data: { "tickers": ["005930", ...] } 또는 { "ticker": "005930" }"""
    sid = request.sid
    tickers = data.get("tickers") or []
    if not tickers and data.get("ticker"):
        tickers = [data["ticker"]]

    removed = []
    for t in tickers:
        with _subs_lock:
            subs = _subscriptions.get(sid, set())
            subs.discard(t)
        leave_room("stock:" + t)
        removed.append(t)

    print("  [WS] 구독해제 (%s): %s" % (sid[:8], removed))
    emit("unsubscribed", {"tickers": removed})


@socketio.on("subscribe_all")
def handle_subscribe_all():
    """전체 종목 구독"""
    handle_subscribe({"tickers": list(DEFAULT_STOCKS.keys())})


@socketio.on("request_refresh")
def handle_request_refresh(data=None):
    """클라이언트에서 수동 새로고침 요청"""
    sid = request.sid
    with _subs_lock:
        tickers = list(_subscriptions.get(sid, set()))

    if not tickers:
        tickers = list(DEFAULT_STOCKS.keys())

    # 캐시 무효화 후 최신 데이터 전송
    for t in tickers:
        with _cache_lock:
            _cache.pop(t, None)

    for t in tickers:
        try:
            rows = get_stock_data(t)
            summary = _build_stock_summary(t, DEFAULT_STOCKS[t], rows)
            emit("stock_update", summary)
        except Exception as e:
            emit("error", {"ticker": t, "message": str(e)})

    emit("refresh_complete", {
        "tickers": tickers,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def _realtime_broadcast():
    """백그라운드 스레드: 주기적으로 구독된 종목 데이터를 브로드캐스트"""
    global _last_signals
    print("  [RT] 실시간 브로드캐스트 시작 (주기: %d초)" % REALTIME_INTERVAL)

    while True:
        socketio.sleep(REALTIME_INTERVAL)

        # 현재 구독 중인 모든 종목 수집
        with _subs_lock:
            active_tickers = set()
            for subs in _subscriptions.values():
                active_tickers.update(subs)

        if not active_tickers:
            continue

        for ticker in active_tickers:
            try:
                # 캐시 무효화 후 최신 데이터 가져오기
                with _cache_lock:
                    _cache.pop(ticker, None)
                rows = get_stock_data(ticker)
                summary = _build_stock_summary(ticker, DEFAULT_STOCKS[ticker], rows)

                # room 단위로 브로드캐스트 (해당 종목 구독자에게만)
                socketio.emit("stock_update", summary, room="stock:" + ticker)

                # 시그널 변경 감지 → 알림
                new_signal = summary["signal"]
                old_signal = _last_signals.get(ticker)
                if old_signal and old_signal != new_signal:
                    alert = {
                        "ticker":     ticker,
                        "name":       DEFAULT_STOCKS[ticker],
                        "old_signal": old_signal,
                        "new_signal": new_signal,
                        "close":      summary["close"],
                        "at":         summary["updated"],
                    }
                    socketio.emit("signal_change", alert, room="stock:" + ticker)
                    print("  [RT] 시그널 변경: %s %s → %s" % (ticker, old_signal, new_signal))

                _last_signals[ticker] = new_signal

            except Exception as e:
                print("  [RT ERROR] %s: %s" % (ticker, str(e)[:80]))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print("=" * 50)
    print("  TREND TRACKER API v4.0 (실시간 WebSocket)")
    print("  소스:", DATA_SOURCE)
    print("  실시간 주기: %d초" % REALTIME_INTERVAL)
    print("  http://0.0.0.0:%d" % port)
    print("=" * 50)
    socketio.start_background_task(_realtime_broadcast)
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
