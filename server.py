"""
TREND TRACKER Flask API — 한국투자증권 실시간 연동
Render.com 무료 배포용

환경변수 (Render 대시보드):
  KIS_APP_KEY     한국투자증권 앱키
  KIS_APP_SECRET  한국투자증권 앱시크릿
  KIS_IS_PAPER    모의투자 여부 (true/false, 기본 false)

수정 이력:
  - cache_get: .seconds → .total_seconds() (CRITICAL: TTL 오동작 수정)
  - api_stocks: KIS 실시간 호출 concurrent.futures로 병렬화 (타임아웃 방지)
  - /api/ai-comment 엔드포인트 추가 (CRITICAL: 브라우저 CORS 우회)
"""

import os
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# ── CORS ─────────────────────────────────────────────────────────
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

# ── KIS 클라이언트 ────────────────────────────────────────────────
try:
    from kis_client import get_monthly_ohlcv, get_current_price
    KIS_AVAILABLE = bool(
        os.environ.get("KIS_APP_KEY") and
        os.environ.get("KIS_APP_SECRET")
    )
except ImportError:
    KIS_AVAILABLE = False

DATA_SOURCE = "kis" if KIS_AVAILABLE else "sample"
print("  데이터 소스:", DATA_SOURCE)

# ── 종목 목록 ─────────────────────────────────────────────────────
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

# ── 캐시 ─────────────────────────────────────────────────────────
_cache      = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 3600


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        # FIX: .seconds → .total_seconds()
        # timedelta.seconds는 days 부분을 무시해 TTL이 비정상 동작
        elapsed = (datetime.now() - item["ts"]).total_seconds()
        if elapsed > CACHE_TTL:
            del _cache[key]
            return None
        return item["data"]


def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": datetime.now()}


# ── 샘플 데이터 (fallback) ────────────────────────────────────────
def _sample_ohlcv(ticker, months=36):
    random.seed(int(ticker) % 99991)
    price = 20000 + random.random() * 60000
    today = date.today()
    rows  = []
    for i in range(months, 0, -1):
        yr  = today.year  + (today.month - i - 1) // 12
        mo  = (today.month - i - 1) % 12 + 1
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


# ── 데이터 수집 ───────────────────────────────────────────────────
def fetch_ohlcv(ticker, months=36):
    if KIS_AVAILABLE:
        try:
            rows = get_monthly_ohlcv(ticker, months)
            print("  [KIS] %s — %d rows" % (ticker, len(rows)))
            return rows
        except Exception as e:
            print("  [KIS ERROR] %s: %s" % (ticker, str(e)[:80]))
    return _sample_ohlcv(ticker, months)


# ── 기술적 분석 ───────────────────────────────────────────────────
def rolling_mean(arr, n, i):
    return sum(arr[i-n+1:i+1]) / n if i >= n - 1 else None


def analyze(rows):
    closes = [r["close"] for r in rows]
    for i, row in enumerate(rows):
        row["ma5"]  = round(rolling_mean(closes, 5,  i)) if i >= 4  else None
        row["ma10"] = round(rolling_mean(closes, 10, i)) if i >= 9  else None
        row["ma20"] = round(rolling_mean(closes, 20, i)) if i >= 19 else None
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
        c = row["close"]
        if   c > ma5 and ma5 > ma10 > ma20 and row["forking"]: row["signal"] = "BUY"
        elif c < ma5 and ma5 < ma10 < ma20:                    row["signal"] = "SELL"
        elif ma5 > ma10 > ma20:                                 row["signal"] = "HOLD"
        else:                                                   row["signal"] = "WAIT"
    return rows


def get_stock_data(ticker):
    cached = cache_get(ticker)
    if cached:
        return cached
    rows = fetch_ohlcv(ticker, 36)
    rows = analyze(rows)
    cache_set(ticker, rows)
    return rows


# ── API 라우트 ────────────────────────────────────────────────────
@app.route("/api/stocks")
def api_stocks():
    # FIX: ThreadPoolExecutor로 KIS 호출 병렬화 — 순차 호출 시 타임아웃 위험
    def _fetch_one(ticker_name):
        ticker, name = ticker_name
        rows   = get_stock_data(ticker)
        latest = rows[-1]
        prev   = rows[-2] if len(rows) >= 2 else latest
        row = {
            "ticker":     ticker,
            "name":       name,
            "date":       latest["date"],
            "close":      latest["close"],
            "prev":       prev["close"],
            "change":     latest["close"] - prev["close"],
            "change_pct": round((latest["close"]-prev["close"]) / max(prev["close"], 1) * 100, 2),
            "ma5":        latest["ma5"],
            "ma10":       latest["ma10"],
            "ma20":       latest["ma20"],
            "alignment":  latest["alignment"],
            "forking":    latest["forking"],
            "signal":     latest["signal"],
            "source":     DATA_SOURCE,
        }
        # 실시간 현재가 (KIS 연결 시)
        if KIS_AVAILABLE:
            try:
                rt = get_current_price(ticker)
                row["realtime_price"]      = rt["close"]
                row["realtime_change"]     = rt["change"]
                row["realtime_change_pct"] = rt["change_pct"]
            except Exception:
                pass
        return row

    result = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_fetch_one, item): item for item in DEFAULT_STOCKS.items()}
        for fut in as_completed(futures):
            try:
                result.append(fut.result())
            except Exception as e:
                ticker = futures[fut][0]
                print("  [ERROR] %s: %s" % (ticker, e))

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
    rows     = get_stock_data(ticker)
    realtime = {}
    if KIS_AVAILABLE:
        try:
            realtime = get_current_price(ticker)
        except Exception:
            pass
    return jsonify({
        "ok":       True,
        "ticker":   ticker,
        "name":     DEFAULT_STOCKS[ticker],
        "data":     rows,
        "realtime": realtime,
        "source":   DATA_SOURCE,
    })


@app.route("/api/quote/<ticker>")
def api_quote(ticker):
    if not KIS_AVAILABLE:
        return jsonify({"ok": False, "error": "KIS API 미연결"}), 503
    try:
        rt = get_current_price(ticker)
        return jsonify({"ok": True, "ticker": ticker, **rt})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    return jsonify({"ok": True, "data": result,
                    "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/api/ai-comment", methods=["POST"])
def api_ai_comment():
    """
    FIX: AI 분석을 서버에서 프록시 처리
    브라우저에서 Anthropic API 직접 호출 시 CORS 오류 → 서버 프록시로 해결
    """
    import requests as req

    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "")
    if not prompt:
        return jsonify({"ok": False, "error": "prompt 필드 필요"}), 400

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY 환경변수 없음"}), 503

    try:
        res = req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":            "application/json",
                "x-api-key":               anthropic_key,
                "anthropic-version":       "2023-06-01",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        data = res.json()
        text = data.get("content", [{}])[0].get("text", "분석 결과 없음")
        return jsonify({"ok": True, "text": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    with _cache_lock:
        _cache.clear()
    for ticker in DEFAULT_STOCKS:
        get_stock_data(ticker)
    return jsonify({"ok": True, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok":        True,
        "time":      datetime.now().isoformat(),
        "source":    DATA_SOURCE,
        "kis_ready": KIS_AVAILABLE,
        "cached":    len(_cache),
    })


@app.route("/")
def root():
    return jsonify({"service": "TREND TRACKER API", "version": "2.0",
                    "source": DATA_SOURCE, "kis": KIS_AVAILABLE})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print("=" * 50)
    print("  TREND TRACKER API (KIS 버전)")
    print("  소스:", DATA_SOURCE)
    print("  http://0.0.0.0:%d" % port)
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
