# -*- coding: utf-8 -*-
"""
부모님 ETF 대시보드 - 로컬 전용 프로그램
- 보유 종목의 종가와 수익률을 자동으로 채워 줍니다.
- 코스피/코스닥/나스닥/S&P500 지수도 자동으로 가져옵니다.
- 배당 기록과 수익률 추이 그래프를 볼 수 있습니다.
- 인터넷에서 '가격만' 읽어올 뿐, 거래 기능은 전혀 없습니다.
"""

import os
import json
import datetime as dt
import threading

from flask import Flask, jsonify, request, render_template

# 시세 조회 라이브러리
import FinanceDataReader as fdr

# 시세 소스 한 곳이 느리거나 막혀 있어도 화면이 멈추지 않도록,
# 모든 조회를 이 시간(초) 안에 끝내고 안 되면 포기합니다.
FETCH_TIMEOUT = 8


def _fdr_read(symbol, start):
    """FinanceDataReader 조회를 일회용 스레드로 감싸 타임아웃을 겁니다.
    한 소스가 멈춰도 그 스레드만 버려지고 화면은 계속 진행됩니다."""
    box = {}

    def _job():
        try:
            box["df"] = fdr.DataReader(symbol, start)
        except Exception as e:
            box["err"] = type(e).__name__

    th = threading.Thread(target=_job, daemon=True)
    th.start()
    th.join(FETCH_TIMEOUT)
    if th.is_alive():
        print(f"[조회 타임아웃] {symbol}")
        return None
    if "err" in box:
        return None
    return box.get("df")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "holdings.json")

app = Flask(__name__)

# 하루 동안 같은 가격을 반복 조회하지 않도록 메모리에 잠깐 저장해 둡니다.
_price_cache = {}
_cache_date = None


# ----------------------------------------------------------------------
# 데이터 파일 읽기 / 쓰기
# ----------------------------------------------------------------------
def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    # 저장 전에 안전하게 백업본을 하나 남깁니다.
    if os.path.exists(DATA_FILE):
        backup = DATA_FILE + ".bak"
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            old = f.read()
        with open(backup, "w", encoding="utf-8") as f:
            f.write(old)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# 시세 조회 (여러 경로를 순서대로 시도)
# ----------------------------------------------------------------------
def _reset_cache_if_new_day():
    global _cache_date, _price_cache
    today = dt.date.today()
    if _cache_date != today:
        _price_cache = {}
        _cache_date = today


def get_stock_close(code):
    """국내 ETF의 (어제종가, 오늘종가)를 돌려줍니다. 실패하면 (None, None)."""
    _reset_cache_if_new_day()
    if not code:
        return None, None
    if code in _price_cache:
        return _price_cache[code]

    start = (dt.date.today() - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    result = (None, None)
    df = _fdr_read(code, start)
    if df is not None and len(df) >= 1 and "Close" in df.columns:
        closes = df["Close"].dropna().tolist()
        if len(closes) >= 2:
            result = (float(closes[-2]), float(closes[-1]))
        elif len(closes) == 1:
            result = (None, float(closes[-1]))

    _price_cache[code] = result
    return result


# 코드 -> 정식 종목명 사전 (하루 한 번만 만들어 캐시)
_name_map = {}
_name_map_date = None


def _ensure_name_map():
    """국내 ETF 전체 목록을 받아 {코드: 정식명} 사전을 만듭니다. 하루 한 번만."""
    global _name_map, _name_map_date
    today = dt.date.today()
    if _name_map_date == today and _name_map:
        return
    box = {}

    def _job():
        try:
            etfs = fdr.StockListing("ETF/KR")
            name_col = "Name" if "Name" in etfs.columns else etfs.columns[1]
            code_col = "Symbol" if "Symbol" in etfs.columns else etfs.columns[0]
            m = {}
            for _, r in etfs.iterrows():
                m[str(r[code_col]).zfill(6)] = str(r[name_col])
            box["m"] = m
        except Exception as e:
            print(f"[종목명 목록 조회 실패] {type(e).__name__}")

    th = threading.Thread(target=_job, daemon=True)
    th.start()
    th.join(FETCH_TIMEOUT)
    if "m" in box and box["m"]:
        _name_map = box["m"]
        _name_map_date = today


def official_name(code):
    """코드의 정식 종목명. 못 찾으면 None."""
    if not code:
        return None
    _ensure_name_map()
    return _name_map.get(str(code).zfill(6))


def get_index(symbols):
    """지수 하나를 여러 심볼 후보로 시도해서 (어제, 오늘)을 돌려줍니다."""
    _reset_cache_if_new_day()
    key = "IDX:" + symbols[0]
    if key in _price_cache:
        return _price_cache[key]

    start = (dt.date.today() - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    result = (None, None)
    for sym in symbols:
        df = _fdr_read(sym, start)
        if df is not None and len(df) >= 1 and "Close" in df.columns:
            closes = df["Close"].dropna().tolist()
            if len(closes) >= 2:
                result = (float(closes[-2]), float(closes[-1]))
                break
            elif len(closes) == 1:
                result = (None, float(closes[-1]))
                break

    _price_cache[key] = result
    return result


def get_usdkrw():
    """원/달러 환율. 실패하면 None."""
    _reset_cache_if_new_day()
    if "USDKRW" in _price_cache:
        return _price_cache["USDKRW"]
    start = (dt.date.today() - dt.timedelta(days=14)).strftime("%Y-%m-%d")
    val = None
    for sym in ["USD/KRW", "USDKRW=X"]:
        df = _fdr_read(sym, start)
        if df is not None and len(df) and "Close" in df.columns:
            val = float(df["Close"].dropna().iloc[-1])
            break
    _price_cache["USDKRW"] = val
    return val


# 지수별 심볼 후보 (앞에서부터 시도; 국내는 KRX, 해외는 야후 실패 시 stooq)
INDEX_SYMBOLS = {
    "코스피": ["KS11"],
    "코스닥": ["KQ11"],
    "나스닥": ["IXIC", "^IXIC", "NAS@IXIC"],
    "S&P500": ["US500", "S&P500", "^GSPC", "SPX"],
}


# ----------------------------------------------------------------------
# 웹 페이지
# ----------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    """대시보드가 필요한 모든 정보를 한 번에 계산해서 돌려줍니다."""
    data = load_data()

    # 1) 지수
    indices = {}
    for name, syms in INDEX_SYMBOLS.items():
        y, t = get_index(syms)
        change = None
        if y and t:
            change = round((t - y) / y * 100, 2)
        indices[name] = {"어제": y, "오늘": t, "등락률": change}

    # 2) 환율
    usdkrw = get_usdkrw()

    # 3) 계좌별 종목
    account_out = []
    grand_eval = 0.0
    grand_cost = 0.0
    name_changed = False  # 정식명으로 바뀐 게 있으면 파일 저장
    for acc in data["계좌"]:
        rows = []
        acc_eval = 0.0
        acc_cost = 0.0
        for h in acc["종목"]:
            code = (h.get("종목코드") or "").strip()
            qty = h.get("수량") or 0
            avg = h.get("평단가")
            y_close, t_close = get_stock_close(code)

            # 코드가 있으면 정식 종목명으로 자동 교체
            if code:
                off = official_name(code)
                if off and off != h.get("종목명"):
                    old_name = h.get("종목명")
                    h["종목명"] = off
                    name_changed = True
                    # 이 종목으로 기록된 배당의 이름도 같이 갱신
                    for rec in data.get("배당기록", []):
                        if rec.get("계좌명") == acc["계좌명"] and rec.get("종목명") == old_name:
                            rec["종목명"] = off

            def ret(price):
                if price is None or not avg:
                    return None
                return round((price - avg) / avg * 100, 2)

            eval_amt = None
            profit = None
            if t_close is not None and qty:
                eval_amt = t_close * qty
                if avg:
                    profit = (t_close - avg) * qty
                    acc_eval += eval_amt
                    acc_cost += avg * qty

            rows.append({
                "종목명": h["종목명"],
                "종목코드": code,
                "수량": qty,
                "평단가": avg,
                "어제종가": y_close,
                "오늘종가": t_close,
                "어제수익률": ret(y_close),
                "오늘수익률": ret(t_close),
                "평가금액": round(eval_amt) if eval_amt is not None else None,
                "평가손익": round(profit) if profit is not None else None,
                "메모": h.get("메모", ""),
                "코드없음": (code == ""),
            })
        acc_ret = None
        if acc_cost > 0:
            acc_ret = round((acc_eval - acc_cost) / acc_cost * 100, 2)
        grand_eval += acc_eval
        grand_cost += acc_cost
        account_out.append({
            "계좌명": acc["계좌명"],
            "계좌번호": acc.get("계좌번호", ""),
            "설명": acc.get("설명", ""),
            "종목": rows,
            "평가금액합": round(acc_eval) if acc_eval else 0,
            "수익률": acc_ret,
        })

    total_ret = None
    if grand_cost > 0:
        total_ret = round((grand_eval - grand_cost) / grand_cost * 100, 2)

    payload = {
        "업데이트시각": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "지수": indices,
        "환율": usdkrw,
        "계좌": account_out,
        "총평가금액": round(grand_eval) if grand_eval else 0,
        "총수익률": total_ret,
        "배당기록": data.get("배당기록", []),
    }

    # 오늘 종목별 수익률을 자동 기록 (그날 마지막 값으로 갱신)
    snapshot_changed = _record_snapshot(data, payload)

    # 이름 변경/스냅샷 변경이 있으면 저장
    if name_changed or snapshot_changed:
        save_data(data)

    payload["수익률스냅샷"] = data.get("수익률스냅샷", {})
    return jsonify(payload)


def _record_snapshot(data, payload):
    """오늘 날짜로 종목별·계좌별 수익률을 저장. 이미 있으면 마지막 값으로 갱신.
    단, 국내 장이 열린 날(코스피 최신 거래일 = 오늘)에만 기록한다.
    구조: data['수익률스냅샷'] = { '날짜': {'총수익률':.., '계좌':{계좌명:수익률},
                                         '종목':{계좌명||종목명: 수익률}} }
    반환: 변경이 있었으면 True"""
    today = dt.date.today().strftime("%Y-%m-%d")

    # 장이 열린 날인지 확인: 코스피 최신 거래일이 오늘인지
    if not _is_market_open_today():
        return False

    snaps = data.get("수익률스냅샷")
    # 예전(리스트) 구조면 새 구조(dict)로 초기화
    if not isinstance(snaps, dict):
        snaps = {}

    day = {"총수익률": payload["총수익률"], "계좌": {}, "종목": {}}
    has_value = False
    for acc in payload["계좌"]:
        day["계좌"][acc["계좌명"]] = acc["수익률"]
        for h in acc["종목"]:
            if h["오늘수익률"] is not None:
                key = acc["계좌명"] + "||" + h["종목명"]
                day["종목"][key] = h["오늘수익률"]
                has_value = True

    if not has_value and payload["총수익률"] is None:
        # 아직 아무 값도 못 구했으면 기록하지 않음 (빈 날짜 방지)
        return False

    snaps[today] = day  # 그날 마지막 값으로 갱신
    data["수익률스냅샷"] = snaps
    return True


def _is_market_open_today():
    """오늘 국내 증시가 열렸는지. 코스피 최신 거래일이 오늘이면 True.
    주말·공휴일이면 최신 거래일이 과거라 False. 조회 실패 시 안전하게 False(기록 안 함)."""
    today = dt.date.today()
    # 주말이면 바로 제외
    if today.weekday() >= 5:  # 5=토, 6=일
        return False
    start = (today - dt.timedelta(days=10)).strftime("%Y-%m-%d")
    df = _fdr_read("KS11", start)
    try:
        if df is not None and len(df):
            last_date = df.index[-1]
            # last_date가 오늘과 같은 날짜인지
            last_str = last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)[:10]
            return last_str == today.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"[장 개장 확인 실패] {e}")
    return False


@app.route("/api/holding", methods=["POST"])
def api_update_holding():
    """한 종목의 수량/평단/코드/메모를 수정합니다. (추가매수·매도 반영)"""
    body = request.get_json(force=True)
    data = load_data()
    acc_name = body.get("계좌명")
    stock_name = body.get("종목명")
    for acc in data["계좌"]:
        if acc["계좌명"] != acc_name:
            continue
        for h in acc["종목"]:
            if h["종목명"] == stock_name:
                if "수량" in body:
                    h["수량"] = body["수량"]
                if "평단가" in body:
                    h["평단가"] = body["평단가"]
                if "종목코드" in body:
                    h["종목코드"] = (body["종목코드"] or "").strip()
                    _price_cache.pop(h["종목코드"], None)
                if "메모" in body:
                    h["메모"] = body["메모"]
                if body.get("새종목명"):
                    h["종목명"] = body["새종목명"]
                save_data(data)
                return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "종목을 찾지 못했습니다."}), 404


@app.route("/api/holding/add", methods=["POST"])
def api_add_holding():
    """새 종목을 추가합니다."""
    body = request.get_json(force=True)
    data = load_data()
    for acc in data["계좌"]:
        if acc["계좌명"] == body.get("계좌명"):
            acc["종목"].append({
                "종목명": body.get("종목명", "새 종목"),
                "종목코드": (body.get("종목코드") or "").strip(),
                "수량": body.get("수량", 0),
                "평단가": body.get("평단가"),
                "메모": body.get("메모", ""),
            })
            save_data(data)
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


@app.route("/api/holding/delete", methods=["POST"])
def api_delete_holding():
    body = request.get_json(force=True)
    data = load_data()
    for acc in data["계좌"]:
        if acc["계좌명"] == body.get("계좌명"):
            acc["종목"] = [h for h in acc["종목"] if h["종목명"] != body.get("종목명")]
            save_data(data)
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


@app.route("/api/holding/buy", methods=["POST"])
def api_buy():
    """추가 매수: 수량을 늘리고 평단가를 가중평균으로 다시 계산합니다.
    입력: 매수수량, 그리고 (매수단가) 또는 (매수금액) 중 하나."""
    body = request.get_json(force=True)
    data = load_data()
    add_qty = float(body.get("매수수량") or 0)
    buy_price = body.get("매수단가")
    buy_amount = body.get("매수금액")

    if add_qty <= 0:
        return jsonify({"ok": False, "error": "매수 수량을 넣어 주세요."}), 400
    # 매수단가가 없고 매수금액만 있으면 단가 = 금액 / 수량
    if buy_price in (None, "", 0):
        if buy_amount in (None, "", 0):
            return jsonify({"ok": False, "error": "매수단가 또는 매수금액이 필요합니다."}), 400
        buy_price = float(buy_amount) / add_qty
    else:
        buy_price = float(buy_price)

    for acc in data["계좌"]:
        if acc["계좌명"] != body.get("계좌명"):
            continue
        for h in acc["종목"]:
            if h["종목명"] != body.get("종목명"):
                continue
            old_qty = float(h.get("수량") or 0)
            old_avg = h.get("평단가")
            new_qty = old_qty + add_qty
            if old_avg in (None, "", 0) or old_qty <= 0:
                # 기존 평단이 없으면 이번 매수단가가 곧 평단
                new_avg = buy_price
            else:
                new_avg = (old_qty * float(old_avg) + add_qty * buy_price) / new_qty
            h["수량"] = int(new_qty) if float(new_qty).is_integer() else new_qty
            h["평단가"] = round(new_avg, 2)
            save_data(data)
            return jsonify({"ok": True, "수량": h["수량"], "평단가": h["평단가"]})
    return jsonify({"ok": False, "error": "종목을 찾지 못했습니다."}), 404


@app.route("/api/holding/sell", methods=["POST"])
def api_sell():
    """매도: 수량만 줄입니다. 평단가는 그대로 둡니다(매도는 평단을 바꾸지 않음)."""
    body = request.get_json(force=True)
    data = load_data()
    sell_qty = float(body.get("매도수량") or 0)
    if sell_qty <= 0:
        return jsonify({"ok": False, "error": "매도 수량을 넣어 주세요."}), 400
    for acc in data["계좌"]:
        if acc["계좌명"] != body.get("계좌명"):
            continue
        for h in acc["종목"]:
            if h["종목명"] != body.get("종목명"):
                continue
            old_qty = float(h.get("수량") or 0)
            if sell_qty > old_qty:
                return jsonify({"ok": False, "error": f"보유 수량({int(old_qty)})보다 많이 팔 수 없습니다."}), 400
            new_qty = old_qty - sell_qty
            h["수량"] = int(new_qty) if float(new_qty).is_integer() else new_qty
            save_data(data)
            return jsonify({"ok": True, "수량": h["수량"]})
    return jsonify({"ok": False, "error": "종목을 찾지 못했습니다."}), 404


@app.route("/api/holding/reorder", methods=["POST"])
def api_reorder():
    """한 계좌 안에서 종목 순서를 바꿉니다. 종목명 리스트 순서대로 정렬합니다."""
    body = request.get_json(force=True)
    data = load_data()
    order = body.get("순서", [])
    for acc in data["계좌"]:
        if acc["계좌명"] == body.get("계좌명"):
            idx = {name: i for i, name in enumerate(order)}
            acc["종목"].sort(key=lambda h: idx.get(h["종목명"], 999))
            save_data(data)
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


@app.route("/api/search_etf")
def api_search_etf():
    """종목명 일부로 국내 ETF 코드를 찾아 줍니다. (부모님 PC에서 인터넷 연결 시 동작)"""
    q = (request.args.get("q") or "").strip().lower().replace(" ", "")
    if not q:
        return jsonify({"ok": True, "결과": []})
    try:
        etfs = fdr.StockListing("ETF/KR")
        # 컬럼명이 버전에 따라 다를 수 있어 유연하게 처리
        name_col = "Name" if "Name" in etfs.columns else etfs.columns[1]
        code_col = "Symbol" if "Symbol" in etfs.columns else etfs.columns[0]
        hits = []
        for _, r in etfs.iterrows():
            nm = str(r[name_col])
            if q in nm.lower().replace(" ", ""):
                hits.append({"코드": str(r[code_col]), "이름": nm})
            if len(hits) >= 15:
                break
        return jsonify({"ok": True, "결과": hits})
    except Exception as e:
        return jsonify({"ok": False, "error": f"목록 조회 실패(인터넷 확인): {e}", "결과": []})


@app.route("/api/dividend/add", methods=["POST"])
def api_add_dividend():
    """배당 기록을 추가합니다."""
    body = request.get_json(force=True)
    data = load_data()
    data.setdefault("배당기록", []).append({
        "날짜": body.get("날짜"),
        "계좌명": body.get("계좌명", ""),
        "종목명": body.get("종목명", ""),
        "금액": body.get("금액", 0),
    })
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/dividend/delete", methods=["POST"])
def api_delete_dividend():
    body = request.get_json(force=True)
    idx = body.get("index")
    data = load_data()
    recs = data.get("배당기록", [])
    if isinstance(idx, int) and 0 <= idx < len(recs):
        recs.pop(idx)
        save_data(data)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    """'오늘 수익률 기록' 버튼 — 지금 값으로 오늘 스냅샷을 강제 갱신합니다."""
    data = load_data()
    with app.test_request_context():
        payload = api_data().get_json()
    # api_data 안에서 이미 자동 저장되므로, 최신 파일을 다시 읽어 확인만 반환
    data = load_data()
    today = dt.date.today().strftime("%Y-%m-%d")
    return jsonify({"ok": True, "저장됨": data.get("수익률스냅샷", {}).get(today)})


if __name__ == "__main__":
    import webbrowser
    import threading

    url = "http://127.0.0.1:5000"
    # 서버가 뜨면 브라우저를 자동으로 엽니다.
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print("=" * 50)
    print("  부모님 ETF 대시보드가 실행되었습니다.")
    print("  브라우저가 자동으로 열립니다.")
    print(f"  안 열리면 이 주소로 접속하세요:  {url}")
    print("  끄려면 이 검은 창을 닫으면 됩니다.")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)
