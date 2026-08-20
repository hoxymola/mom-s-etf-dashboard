# -*- coding: utf-8 -*-
"""
부모님 자산 대시보드 - 로컬 전용 프로그램
- 보유 종목(ETF·개별주식)의 종가와 수익률을 자동으로 채워 줍니다.
- 코스피/코스닥/나스닥/S&P500 지수도 자동으로 가져옵니다.
- 배당 기록과 수익률 추이 그래프를 볼 수 있습니다.
- 인터넷에서 '가격만' 읽어올 뿐, 거래 기능은 전혀 없습니다.
"""

import os
import re
import json
import logging
import datetime as dt
import threading

from flask import Flask, jsonify, request, render_template

# 시세 조회 라이브러리
import FinanceDataReader as fdr
# 배당(분배금) 이력 조회 라이브러리
# (국내 종목은 코드 뒤에 시장 접미사를 붙여 조회 - 코스피 .KS / 코스닥 .KQ)
import yfinance as yf

# yfinance가 검은 창에 쏟아 내는 경고를 끕니다. ("Period 'max' is invalid",
# "possibly delisted" 등) 부모님이 보시는 화면이라 놀라지 않게 조용히 두고,
# 필요한 안내는 아래에서 우리 문장으로 직접 출력합니다.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

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
DATA_TEMPLATE_FILE = os.path.join(BASE_DIR, "data", "holdings.example.json")

app = Flask(__name__)

# 하루 동안 같은 가격을 반복 조회하지 않도록 메모리에 잠깐 저장해 둡니다.
_price_cache = {}
_cache_date = None


# ----------------------------------------------------------------------
# 데이터 파일 읽기 / 쓰기
# ----------------------------------------------------------------------
def load_data():
    # 처음 실행하는 컴퓨터라 holdings.json이 없으면, 빈 틀(예시 템플릿)로 새로 만듭니다.
    if not os.path.exists(DATA_FILE):
        with open(DATA_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            template = f.read()
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            f.write(template)
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
    """국내 상장 종목(ETF·개별주식)의 (어제종가, 오늘종가)를 돌려줍니다.
    실패하면 (None, None)."""
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


# 하루 동안 같은 배당(분배금) 이력을 반복 조회하지 않도록 메모리에 잠깐 저장해 둡니다.
_dividend_cache = {}
_dividend_cache_date = None


def _reset_dividend_cache_if_new_day():
    global _dividend_cache_date, _dividend_cache
    today = dt.date.today()
    if _dividend_cache_date != today:
        _dividend_cache = {}
        _dividend_cache_date = today


def get_dividend_history(code):
    """종목코드의 배당(분배금) 이력을 {배당락일: 1주당 금액} 형태로 돌려줍니다.
    실패하거나 이력이 없으면 빈 dict. (yfinance는 API 키가 필요 없지만,
    일부 최근 상장 종목은 이력이 비어 있을 수 있습니다.)"""
    _reset_dividend_cache_if_new_day()
    if not code:
        return {}
    if code in _dividend_cache:
        return _dividend_cache[code]

    box = {}
    suffixes = _yf_suffixes(code)  # 스레드 밖에서 미리 정해 둔다 (목록 조회 포함)

    def _job():
        unavailable = False  # 조회 자체가 안 된 경우 (배당이 없는 종목과 구분)
        for suffix in suffixes:
            # 조회가 실패하면 yfinance가 None을 주기도 하므로(빈 이력과 구분 안 됨)
            # 변환까지 한 덩어리로 감싸 둔다. 여기서 예외가 새면 스레드가 죽는다.
            try:
                div = yf.Ticker(code + suffix).dividends
                if div is None:
                    unavailable = True
                    continue
                if not len(div):
                    continue  # 배당이 없는 종목 -> 다음 접미사 시도
                box["div"] = {ts.date(): float(v) for ts, v in div.items()}
                return
            except Exception as e:
                unavailable = True
                print(f"[배당 조회 실패] {code}{suffix}: {type(e).__name__}")
        if unavailable:
            print(f"[배당 자동조회 불가] {code} - 화면에서 '배당/주' 칸에 "
                  f"1주당 배당금을 직접 넣어 주세요.")

    th = threading.Thread(target=_job, daemon=True)
    th.start()
    th.join(FETCH_TIMEOUT)
    result = box.get("div", {})
    _dividend_cache[code] = result
    return result


# 코드 -> 정식 종목명 / 상장시장 사전 (하루 한 번만 만들어 캐시)
# ETF와 개별주식(코스피·코스닥·코넥스)을 한 사전에 모아 둡니다.
_name_map = {}
_market_map = {}
# ETF 목록의 Category 숫자 (1 국내지수 / 2 국내테마 / 3 국내파생 / 4 해외주식 /
# 5 원자재 / 6 채권 / 7 혼합·기타). 섹터 자동분류의 마지막 폴백으로만 씁니다.
_category_map = {}
_listing_date = None

# (조회할 목록, 그 목록에 Market 컬럼이 없을 때 대신 쓸 시장 이름)
# ETF/KR 목록에는 시장 컬럼이 없지만 국내 ETF는 코스피 상장이라 .KS를 씁니다.
LISTING_SOURCES = (("ETF/KR", "KOSPI"), ("KRX", None))


def _ensure_listing():
    """국내 ETF + 개별주식 전체 목록을 받아 {코드: 정식명}·{코드: 시장} 사전을
    만듭니다. 하루 한 번만. (ETF 약 1,100개 + 주식 약 2,900개)"""
    global _name_map, _market_map, _category_map, _listing_date
    today = dt.date.today()
    if _listing_date == today and _name_map:
        return
    box = {}

    def _job():
        names, markets, categories = {}, {}, {}
        for source, default_market in LISTING_SOURCES:
            try:
                df = fdr.StockListing(source)
            except Exception as e:
                print(f"[종목 목록 조회 실패] {source}: {type(e).__name__}")
                continue
            # 컬럼명이 목록·버전에 따라 다를 수 있어 유연하게 처리
            name_col = "Name" if "Name" in df.columns else df.columns[1]
            code_col = next((c for c in ("Symbol", "Code") if c in df.columns), df.columns[0])
            has_market = "Market" in df.columns
            has_category = "Category" in df.columns
            for _, r in df.iterrows():
                code = str(r[code_col]).zfill(6)
                names[code] = str(r[name_col])
                markets[code] = str(r["Market"]) if has_market else default_market
                if has_category:
                    try:
                        categories[code] = int(r["Category"])
                    except (TypeError, ValueError):
                        pass
        if names:
            box["names"], box["markets"], box["categories"] = names, markets, categories

    th = threading.Thread(target=_job, daemon=True)
    th.start()
    th.join(FETCH_TIMEOUT)
    if box.get("names"):
        # 한 목록만 성공해도 그만큼은 쓴다. 둘 다 실패하면 어제 사전을 그대로 유지.
        _name_map = box["names"]
        _market_map = box["markets"]
        _category_map = box["categories"]
        _listing_date = today


def norm_code(code):
    """종목코드 표기 통일. 앞뒤 공백을 떼고 대문자로 맞춥니다.
    (최근 상장 ETF 코드에는 영문이 섞여 있고 - 예: 0094M0 - 목록 사전은
    대문자 기준이라, 소문자로 들어오면 이름·시장 조회가 어긋납니다.)"""
    return (code or "").strip().upper()


def official_name(code):
    """코드의 정식 종목명. 못 찾으면 None."""
    if not code:
        return None
    _ensure_listing()
    return _name_map.get(norm_code(code).zfill(6))


def etf_category(code):
    """ETF 목록에 적힌 Category 숫자. ETF가 아니거나 못 찾으면 None."""
    if not code:
        return None
    _ensure_listing()
    return _category_map.get(norm_code(code).zfill(6))


def _yf_suffixes(code):
    """yfinance 티커에 붙일 접미사 후보. 코스닥(코스닥 글로벌 포함)은 .KQ,
    코스피·ETF는 .KS. 시장을 못 찾으면(코넥스·목록 조회 실패 등) 둘 다 시도합니다."""
    _ensure_listing()
    market = (_market_map.get(norm_code(code).zfill(6)) or "").upper()
    if "KOSDAQ" in market:
        return (".KQ",)
    if "KOSPI" in market:
        return (".KS",)
    return (".KS", ".KQ")


# ----------------------------------------------------------------------
# 섹터(테마) 분류
# ----------------------------------------------------------------------
# 종목코드만으로는 섹터를 알 수 없습니다. 국내 주식 목록(KRX)에는 업종 컬럼이
# 아예 없고, ETF 목록(ETF/KR)의 Category는 '국내 업종·테마'처럼 뭉뚱그려져 있어
# 반도체 ETF와 고배당 ETF가 같은 값(2)으로 들어옵니다.
# 그래서 'ETF 이름에 테마가 그대로 적혀 있다'는 점을 이용해 이름으로 분류합니다.
#
# 위에서부터 먼저 걸리는 규칙이 이깁니다. 순서가 중요합니다 —
#   '미국우주항공'은 미국지수보다 우주항공이 먼저 걸려야 하고,
#   '금융고배당'은 원자재(금)가 아니라 배당·밸류로 가야 하고,
#   '200헬스케어'는 국내지수('200')가 아니라 바이오·헬스로 가야 합니다.
# 그래서 업종·테마 규칙이 모두 위에 있고, 넓게 걸리는 지수 규칙이 맨 아래입니다.
# '은행·금융'에 '금융'을 넣지 않은 것도 같은 이유입니다 —
# '금융고배당TOP10'을 은행 ETF로 잘못 끌어가지 않도록.
# 자동 분류가 어색한 종목은 화면에서 섹터를 눌러 직접 지정할 수 있고,
# 그 값(holdings.json의 '섹터')이 이 규칙보다 우선합니다.
SECTOR_RULES = (
    ("채권혼합", ("채권혼합", "밸런스", "미국채커버드콜", "미국채혼합", "국채혼합", "채권 혼합")),
    ("채권", ("국채", "채권", "CD금리", "KOFR", "머니마켓", "통안채")),
    ("원자재", ("국제금", "금현물", "금선물", "골드", "GOLD", "은현물", "은선물",
                "은액티브", "실버", "SILVER", "원유", "구리", "농산물")),
    ("우주항공", ("우주항공", "항공우주", "방산", "방위산업")),
    ("원자력", ("원자력", "원전", "SMR")),
    ("반도체", ("반도체", "삼성전자", "SK하이닉스", "하이닉스", "엔비디아", "마이크론")),
    ("2차전지", ("2차전지", "이차전지", "배터리", "리튬", "에너지솔루션")),
    ("자동차", ("자동차", "현대차", "기아", "전기차", "모빌리티")),
    ("바이오·헬스", ("바이오", "헬스케어", "헬스", "제약", "의료기기", "의료")),
    ("조선·해운", ("조선", "해운", "운송")),
    ("은행·금융", ("은행", "보험", "증권")),
    ("리츠·부동산", ("리츠", "부동산", "인프라")),
    ("소비재", ("화장품", "음식료", "유통", "소비재", "필수소비")),
    ("게임·엔터", ("게임", "엔터", "미디어", "콘텐츠")),
    ("AI·빅테크", ("AI", "빅테크", "테크100", "테크10", "데이터센터", "광통신",
                   "미국성장", "성장커버드콜", "테슬라", "애플", "M7", "빅7", "7+")),
    ("미국지수", ("나스닥100", "S&P500", "미국500", "미국S&P", "미국주식", "미국시장")),
    ("배당·밸류", ("고배당", "주주환원", "밸류업", "배당", "금융", "가치")),
    ("국내지수", ("코스피", "코스닥", "코리아", "200")),
)

# 이름 규칙에 하나도 안 걸릴 때만 쓰는 폴백. ETF 목록의 Category 숫자입니다.
ETF_CATEGORY_SECTOR = {
    1: "국내지수", 2: "국내테마", 3: "국내파생",
    4: "해외주식", 5: "원자재", 6: "채권", 7: "혼합·기타",
}

# 화면의 섹터 선택 목록 (직접 지정할 때 고르는 값)
SECTOR_CHOICES = tuple(s for s, _ in SECTOR_RULES) + ("해외주식", "국내테마", "기타")


def _kw_hit(name_up, kw_up):
    """키워드가 종목명에 들어 있는지. 영문 약어(AI·GOLD 등)는 다른 영단어 속에
    우연히 끼어 있는 경우를 걸러내려고 앞뒤가 영문자가 아닐 때만 인정합니다.
    ('DAILY' 안의 'AI'를 AI 테마로 잘못 잡지 않도록)"""
    if kw_up.isascii() and any(c.isalpha() for c in kw_up):
        return re.search(r"(?<![A-Za-z])" + re.escape(kw_up) + r"(?![A-Za-z])", name_up) is not None
    return kw_up in name_up


def classify_sector(name, code=""):
    """종목명 키워드로 섹터를 정합니다. 규칙에 안 걸리면 ETF 목록의 Category로
    대분류를 매기고, 그것도 없으면 '기타'."""
    name_up = (name or "").upper()
    for sector, keywords in SECTOR_RULES:
        for kw in keywords:
            if _kw_hit(name_up, kw.upper()):
                return sector
    return ETF_CATEGORY_SECTOR.get(etf_category(code), "기타")


def sector_options(rows):
    """섹터 지정 창에 보여 줄 목록. 규칙에 있는 섹터 + 지금 실제로 쓰이고 있는 섹터.
    직접 적어 넣은 이름도 한 번 쓰이면 다음부터 목록에 나옵니다."""
    known = list(SECTOR_CHOICES)
    extra = sorted({r["섹터"] for r in rows if r["섹터"] not in known})
    return known + extra


def sector_summary(rows, base_amount):
    """종목 목록을 섹터별로 묶어 [{섹터, 평가금액, 비중, 종목수}] 를 만듭니다.
    평가금액이 큰 섹터부터 정렬합니다."""
    box = {}
    for r in rows:
        b = box.setdefault(r["섹터"], {"섹터": r["섹터"], "평가금액": 0, "종목수": 0})
        b["평가금액"] += r.get("평가금액") or 0
        b["종목수"] += 1
    out = sorted(box.values(), key=lambda x: -x["평가금액"])
    for b in out:
        b["비중"] = round(b["평가금액"] / base_amount * 100, 1) if base_amount else None
    return out


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
    # 비중 계산용 합계. 위의 grand_eval 은 '평단가가 있는 종목'만 더하지만
    # (수익률 기준이 없으면 빼야 하니까), 비중은 수익률과 무관하므로
    # '평가금액이 있는 종목 전부'를 분모로 씁니다.
    grand_eval_all = 0.0
    name_changed = False  # 정식명으로 바뀐 게 있으면 파일 저장

    # 배당금 표시는 '현재 수량'이 아니라 '그 배당이 배당기록에 기록됐을 때의 수량' 기준으로 고정한다.
    # (계좌명, 종목명, 날짜) -> 그때 저장된 금액
    dividend_amount_by_key = {
        (r.get("계좌명"), r.get("종목명"), r.get("날짜")): r.get("금액")
        for r in data.get("배당기록", [])
    }

    for acc in data["계좌"]:
        rows = []
        acc_eval = 0.0
        acc_cost = 0.0
        acc_eval_all = 0.0
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
                acc_eval_all += eval_amt
                if avg:
                    profit = (t_close - avg) * qty
                    acc_eval += eval_amt
                    acc_cost += avg * qty

            # 가장 최근 1주당 배당금(분배금)·배당일자, 및 보유 수량 기준 배당금
            # 조회값이 실제와 다를 때를 대비해, 사용자가 '주당배당금수정'을 직접 입력해 두면
            # 조회값 대신 그 값을 우선 사용한다. (초기화하면 다시 조회값을 씀)
            div_date = None
            div_per_share = None
            if code:
                div_hist = get_dividend_history(code)
                if div_hist:
                    last_ex_date = max(div_hist)
                    div_date = last_ex_date.strftime("%Y-%m-%d")
                    div_per_share = round(div_hist[last_ex_date])

            div_override = h.get("주당배당금수정")
            if div_override is not None:
                div_per_share = div_override

            # 배당기록에 이미 고정된 금액이 있으면 그 값을 그대로 쓰고(수량이 나중에 바뀌어도 불변),
            # 아직 기록되지 않았을 때만(수정값 사용 중이거나 신규 배당 감지 전) 현재 수량으로 잠정 계산한다.
            div_amt = None
            if div_override is None and div_date is not None:
                div_amt = dividend_amount_by_key.get((acc["계좌명"], h["종목명"], div_date))
            if div_amt is None and div_per_share is not None and qty:
                div_amt = round(div_per_share * qty)

            # 섹터: 직접 지정한 값이 있으면 그것, 없으면 종목명으로 자동 분류
            sector_manual = (h.get("섹터") or "").strip() or None
            sector = sector_manual or classify_sector(h["종목명"], code)

            rows.append({
                "종목명": h["종목명"],
                "종목코드": code,
                "섹터": sector,
                "섹터수동": sector_manual,
                "수량": qty,
                "평단가": avg,
                "어제종가": y_close,
                "오늘종가": t_close,
                "어제수익률": ret(y_close),
                "오늘수익률": ret(t_close),
                "배당일자": div_date,
                "주당배당금": div_per_share,
                "주당배당금수정": div_override,
                "배당금": div_amt,
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
        grand_eval_all += acc_eval_all

        # 계좌 안에서의 비중
        for r in rows:
            amt = r.get("평가금액")
            r["계좌내비중"] = (round(amt / acc_eval_all * 100, 1)
                              if amt is not None and acc_eval_all else None)

        account_out.append({
            "계좌명": acc["계좌명"],
            "계좌번호": acc.get("계좌번호", ""),
            "설명": acc.get("설명", ""),
            "종목": rows,
            "평가금액합": round(acc_eval) if acc_eval else 0,
            "수익률": acc_ret,
            "비중기준금액": round(acc_eval_all),
            "섹터집계": sector_summary(rows, acc_eval_all),
        })

    total_ret = None
    if grand_cost > 0:
        total_ret = round((grand_eval - grand_cost) / grand_cost * 100, 2)

    # 전체 자산 대비 비중 + 전체 섹터 집계
    all_rows = [r for acc in account_out for r in acc["종목"]]
    for r in all_rows:
        amt = r.get("평가금액")
        r["전체비중"] = (round(amt / grand_eval_all * 100, 1)
                        if amt is not None and grand_eval_all else None)

    payload = {
        "업데이트시각": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "지수": indices,
        "환율": usdkrw,
        "계좌": account_out,
        "총평가금액": round(grand_eval) if grand_eval else 0,
        "총수익률": total_ret,
        "비중기준금액": round(grand_eval_all),
        "섹터집계": sector_summary(all_rows, grand_eval_all),
        "섹터목록": sector_options(all_rows),
        "배당기록": data.get("배당기록", []),
    }

    # 오늘 종목별 수익률을 자동 기록 (그날 마지막 값으로 갱신)
    snapshot_changed = _record_snapshot(data, payload)

    # 아직 기록되지 않은 새 배당(분배금)이 있으면 자동으로 배당기록에 추가
    dividend_changed = _check_new_dividends(data)

    # 이름 변경/스냅샷 변경/배당 자동기록이 있으면 저장
    if name_changed or snapshot_changed or dividend_changed:
        save_data(data)

    # 계좌별 '이번 달' 받은 배당금 합계 (방금 자동 기록된 배당까지 포함)
    this_month = dt.date.today().strftime("%Y-%m")
    month_total_by_acc = {}
    for rec in data.get("배당기록", []):
        if (rec.get("날짜") or "").startswith(this_month):
            acc_name = rec.get("계좌명")
            month_total_by_acc[acc_name] = month_total_by_acc.get(acc_name, 0) + (rec.get("금액") or 0)
    for acc in account_out:
        acc["이번달배당금"] = round(month_total_by_acc.get(acc["계좌명"], 0))

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


def _check_new_dividends(data):
    """각 보유 종목의 배당(분배금) 이력을 조회해서, 아직 기록되지 않은 새 배당이 있으면
    '배당기록'에 자동으로 추가합니다. 종목별로 이미 기록된 가장 최근 날짜 이후의 배당만
    새로 추가하므로, 과거에 직접 입력해 둔 기록은 건드리지 않습니다.
    날짜는 배당락일(yfinance 기준) 기준이라 실제 입금일과 며칠 차이 날 수 있습니다.
    반환: 새로 추가된 게 있으면 True"""
    changed = False
    records = data.setdefault("배당기록", [])
    existing = {(r.get("계좌명"), r.get("종목명"), r.get("날짜")) for r in records}

    latest_recorded = {}
    for r in records:
        key = (r.get("계좌명"), r.get("종목명"))
        d = r.get("날짜")
        if d and (key not in latest_recorded or d > latest_recorded[key]):
            latest_recorded[key] = d

    for acc in data["계좌"]:
        for h in acc["종목"]:
            code = (h.get("종목코드") or "").strip()
            qty = h.get("수량") or 0
            if not code or not qty:
                continue
            div_hist = get_dividend_history(code)
            if not div_hist:
                continue
            key = (acc["계좌명"], h["종목명"])
            baseline = latest_recorded.get(key)
            if baseline is None:
                # 이 종목은 배당기록이 한 번도 없었던 경우: 과거 이력을 몰아서 채우면
                # (수량이 그때그때 달랐을 수 있어) 부정확하므로, 가장 최근 배당 1건만
                # 추가해 그 시점부터 추적을 시작한다.
                events = sorted(div_hist.items())[-1:]
            else:
                events = [(d, v) for d, v in sorted(div_hist.items())
                          if d.strftime("%Y-%m-%d") > baseline]
            for ex_date, per_share in events:
                date_str = ex_date.strftime("%Y-%m-%d")
                if (acc["계좌명"], h["종목명"], date_str) in existing:
                    continue
                records.append({
                    "날짜": date_str,
                    "계좌명": acc["계좌명"],
                    "종목명": h["종목명"],
                    "금액": round(per_share * qty),
                    "주당분배금": round(per_share),
                    "메모": "자동조회(배당락일 기준)",
                })
                existing.add((acc["계좌명"], h["종목명"], date_str))
                latest_recorded[key] = date_str
                changed = True

    return changed


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


@app.route("/api/account/add", methods=["POST"])
def api_add_account():
    """새 계좌를 추가합니다."""
    body = request.get_json(force=True)
    data = load_data()
    name = (body.get("계좌명") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "계좌 이름을 넣어 주세요."}), 400
    if any(acc["계좌명"] == name for acc in data["계좌"]):
        return jsonify({"ok": False, "error": "이미 있는 계좌 이름입니다."}), 400
    data["계좌"].append({
        "계좌명": name,
        "계좌번호": (body.get("계좌번호") or "").strip(),
        "설명": body.get("설명", ""),
        "종목": [],
    })
    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/account/update", methods=["POST"])
def api_update_account():
    """계좌 이름/번호/설명을 수정합니다. 이름을 바꾸면 배당기록·수익률스냅샷의
    계좌명도 함께 바꿔서 기존 기록이 새 이름으로 계속 이어지게 합니다."""
    body = request.get_json(force=True)
    data = load_data()
    old_name = body.get("계좌명")
    target = next((acc for acc in data["계좌"] if acc["계좌명"] == old_name), None)
    if target is None:
        return jsonify({"ok": False, "error": "계좌를 찾지 못했습니다."}), 404

    new_name = (body.get("새계좌명") or "").strip()
    if new_name and new_name != old_name:
        if any(acc["계좌명"] == new_name for acc in data["계좌"]):
            return jsonify({"ok": False, "error": "이미 있는 계좌 이름입니다."}), 400
        for rec in data.get("배당기록", []):
            if rec.get("계좌명") == old_name:
                rec["계좌명"] = new_name
        for day in data.get("수익률스냅샷", {}).values():
            acc_map = day.get("계좌", {})
            if old_name in acc_map:
                acc_map[new_name] = acc_map.pop(old_name)
            stock_map = day.get("종목", {})
            prefix = old_name + "||"
            for key in list(stock_map.keys()):
                if key.startswith(prefix):
                    stock_map[new_name + "||" + key[len(prefix):]] = stock_map.pop(key)
        target["계좌명"] = new_name

    if "계좌번호" in body:
        target["계좌번호"] = (body["계좌번호"] or "").strip()
    if "설명" in body:
        target["설명"] = body["설명"] or ""

    save_data(data)
    return jsonify({"ok": True})


@app.route("/api/account/delete", methods=["POST"])
def api_delete_account():
    """계좌를 삭제합니다. 그 계좌에 속한 종목·배당기록·수익률스냅샷도 함께 정리합니다."""
    body = request.get_json(force=True)
    data = load_data()
    name = body.get("계좌명")
    if not any(acc["계좌명"] == name for acc in data["계좌"]):
        return jsonify({"ok": False, "error": "계좌를 찾지 못했습니다."}), 404

    data["계좌"] = [acc for acc in data["계좌"] if acc["계좌명"] != name]
    data["배당기록"] = [r for r in data.get("배당기록", []) if r.get("계좌명") != name]
    for day in data.get("수익률스냅샷", {}).values():
        day.get("계좌", {}).pop(name, None)
        prefix = name + "||"
        stock_map = day.get("종목", {})
        for key in list(stock_map.keys()):
            if key.startswith(prefix):
                stock_map.pop(key, None)

    save_data(data)
    return jsonify({"ok": True})


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
                    h["종목코드"] = norm_code(body["종목코드"])
                    _price_cache.pop(h["종목코드"], None)
                if "메모" in body:
                    h["메모"] = body["메모"]
                if "주당배당금수정" in body:
                    if body["주당배당금수정"] is None:
                        h.pop("주당배당금수정", None)  # 초기화 -> 조회값으로 되돌림
                    else:
                        h["주당배당금수정"] = body["주당배당금수정"]
                if "섹터" in body:
                    if body["섹터"]:
                        h["섹터"] = body["섹터"]
                    else:
                        h.pop("섹터", None)  # 초기화 -> 이름으로 자동 분류
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
                "종목코드": norm_code(body.get("종목코드")),
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


@app.route("/api/search_stock")
@app.route("/api/search_etf")  # 예전 주소 (호환용)
def api_search_stock():
    """종목명 일부로 국내 ETF·개별주식 코드를 찾아 줍니다.
    (부모님 PC에서 인터넷 연결 시 동작)"""
    q = (request.args.get("q") or "").strip().lower().replace(" ", "")
    if not q:
        return jsonify({"ok": True, "결과": []})
    _ensure_listing()
    if not _name_map:
        return jsonify({"ok": False, "error": "목록 조회 실패(인터넷 확인)", "결과": []})

    hits = []
    for code, nm in _name_map.items():
        flat = nm.lower().replace(" ", "")
        if q in flat:
            # 검색어로 시작하는 이름을 앞에, 그다음 이름이 짧은 것을 앞에 둔다.
            # ('삼성전자'로 검색했을 때 '삼성전자'가 '삼성전자우'보다 위로 오도록)
            hits.append((0 if flat.startswith(q) else 1, len(nm), nm, code))
    hits.sort()
    return jsonify({"ok": True,
                    "결과": [{"코드": c, "이름": n} for _, _, n, c in hits[:15]]})


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
