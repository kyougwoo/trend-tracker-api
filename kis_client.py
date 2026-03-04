"""
한국투자증권 Open API 클라이언트
KIS Developers: https://apiportal.koreainvestment.com

수정 이력:
  - custtype 헤더 추가 (CRITICAL: 누락 시 일부 엔드포인트 400 오류)
  - reversed(output2 or []) 로 None 안전 처리 (CRITICAL: TypeError 방지)
  - 토큰 캐시 thread-safe Lock 추가
"""

import os
import json
import time
import threading
import requests
from datetime import datetime, date, timedelta

KIS_APP_KEY    = os.environ.get("KIS_APP_KEY",    "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_ACCOUNT_NO = os.environ.get("KIS_ACCOUNT_NO", "")
KIS_IS_PAPER   = os.environ.get("KIS_IS_PAPER", "false").lower() == "true"

BASE_URL = (
    "https://openapivts.koreainvestment.com:29443"
    if KIS_IS_PAPER else
    "https://openapi.koreainvestment.com:9443"
)

# ── 토큰 캐시 (thread-safe) ───────────────────────────────────────
_token_lock  = threading.Lock()
_token_cache = {"token": None, "expires_at": 0}


def get_access_token() -> str:
    """OAuth 2.0 접근 토큰 발급 (캐시 + thread-safe)"""
    now = time.time()
    with _token_lock:
        if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
            return _token_cache["token"]

        if not KIS_APP_KEY or not KIS_APP_SECRET:
            raise RuntimeError(
                "KIS_APP_KEY / KIS_APP_SECRET 환경변수를 설정해 주세요.\n"
                "https://apiportal.koreainvestment.com 에서 앱 등록 후 발급받을 수 있습니다."
            )

        url  = BASE_URL + "/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey":     KIS_APP_KEY,
            "appsecret":  KIS_APP_SECRET,
        }
        res = requests.post(url, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()

        if "access_token" not in data:
            raise RuntimeError("토큰 발급 실패: " + json.dumps(data, ensure_ascii=False))

        _token_cache["token"]      = data["access_token"]
        _token_cache["expires_at"] = now + int(data.get("expires_in", 86400))
        return _token_cache["token"]


def _headers(tr_id: str, is_hash: bool = False) -> dict:
    """
    공통 요청 헤더
    FIX: custtype 추가 — KIS API 일부 엔드포인트 필수 헤더
    """
    h = {
        "Content-Type":  "application/json; charset=utf-8",
        "authorization": "Bearer " + get_access_token(),
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",   # FIX: P=개인, B=법인 — 누락 시 일부 엔드포인트 400 오류
    }
    return h


# ── 현재가 조회 ───────────────────────────────────────────────────
def get_current_price(ticker: str) -> dict:
    """
    국내주식 현재가 시세 조회 (tr_id: FHKST01010100)
    반환: { close, open, high, low, volume, change, change_pct, name }
    """
    url    = BASE_URL + "/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         ticker,
    }
    res = requests.get(url, headers=_headers("FHKST01010100"), params=params, timeout=10)
    res.raise_for_status()
    data = res.json()

    if data.get("rt_cd") != "0":
        raise RuntimeError("현재가 조회 실패 [%s]: %s" % (ticker, data.get("msg1", "")))

    o = data["output"]
    return {
        "close":      int(o["stck_prpr"]),
        "open":       int(o["stck_oprc"]),
        "high":       int(o["stck_hgpr"]),
        "low":        int(o["stck_lwpr"]),
        "volume":     int(o["acml_vol"]),
        "change":     int(o["prdy_vrss"]),
        "change_pct": float(o["prdy_ctrt"]),
        "name":       o.get("hts_kor_isnm", ""),
    }


# ── 월봉 데이터 조회 ──────────────────────────────────────────────
def get_monthly_ohlcv(ticker: str, months: int = 36) -> list:
    """
    국내주식 기간별 시세 조회 — 월봉 (tr_id: FHKST03010100)
    반환: [ { date, open, high, low, close, volume }, ... ]  (오래된 순)
    """
    url      = BASE_URL + "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    today    = date.today()
    start_dt = today - timedelta(days=months * 31 + 90)

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD":         ticker,
        "FID_INPUT_DATE_1":       start_dt.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2":       today.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE":    "M",   # M=월봉
        "FID_ORG_ADJ_PRC":        "0",   # 0=수정주가
    }
    res = requests.get(url, headers=_headers("FHKST03010100"), params=params, timeout=15)
    res.raise_for_status()
    data = res.json()

    if data.get("rt_cd") != "0":
        raise RuntimeError("월봉 조회 실패 [%s]: %s" % (ticker, data.get("msg1", "")))

    # FIX: output2가 None인 경우 TypeError 방지 (None → [])
    raw = data.get("output2") or []

    rows = []
    for item in reversed(raw):   # KIS는 최신순 → reversed로 오래된 순 정렬
        try:
            dt    = item["stck_bsop_date"]     # YYYYMMDD
            label = dt[:4] + "-" + dt[4:6]    # YYYY-MM
            rows.append({
                "date":   label,
                "open":   int(item["stck_oprc"]),
                "high":   int(item["stck_hgpr"]),
                "low":    int(item["stck_lwpr"]),
                "close":  int(item["stck_clpr"]),
                "volume": int(item["acml_vol"]),
            })
        except (KeyError, ValueError):
            continue

    # 같은 월 중복 제거 (마지막 값 유지) → 날짜순 정렬
    seen = {}
    for r in rows:
        seen[r["date"]] = r
    return sorted(seen.values(), key=lambda x: x["date"])[-months:]


def get_stock_name(ticker: str) -> str:
    try:
        return get_current_price(ticker)["name"]
    except Exception:
        return ticker
