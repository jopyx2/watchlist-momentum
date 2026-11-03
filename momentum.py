#!/usr/bin/env python3
"""관심종목 모멘텀 추적기 (PC 에서 실행 -> HTML 한 장).

시세 수집(yfinance) -> 1개월·3개월 수익률(4개 시점) -> 사분면·신호 판정 -> HTML 저장 후 브라우저로 열기.
업종 섹터 모멘텀 추적기와 계산 정의가 같다.

사용법
  python momentum.py                      최신 종가 기준으로 계산하고 HTML 을 브라우저로 연다
  python momentum.py --no-open            HTML 만 만들고 열지 않음
  python momentum.py --asof 2026-10-14    특정 기준일로 다시 계산
  python momentum.py --dump-json 경로     계산 결과를 JSON 으로 저장(검증용)

관심종목은 config.json 의 watchlist 에서 고친다 (미국 종목 티커만).

계산 정의
  - 가격: 배당 미반영 종가 (yfinance auto_adjust=False 의 Close)
  - 1개월·3개월 수익률: 기준일의 같은 날짜 1개월·3개월 전 종가 대비. 그 날이 휴장이면 직전 거래일 종가
  - 궤적 시점: 기준일, 기준일-7일, -14일, -21일 (각 시점마다 1개월·3개월을 다시 계산)
  - 사분면: x=1개월, y=3개월 / 우상단 상승 지속, 좌상단 하락 전환, 우하단 상승 전환, 좌하단 하락 지속
  - 신호: 최근 3주 안에 상승 전환 구간을 거쳐 현재 상승 지속이면 ▲ 상승 진입,
          하락 전환 구간을 거쳐 현재 하락 지속이면 ▼ 하락 진입
  - 속도 변화 = 1개월 수익률 − 3개월 수익률÷3 (%p), ±0.5%p 이내는 '유지'
  - 3개월 전 날짜가 상장일보다 앞서면 그 시점의 3개월 수익률은 없음(상장 3개월 미만 -> 표에만 표시)
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
PACE_BAND = 0.5         # 속도 변화가 ±0.5%p 안이면 '유지'로 본다 (페이지 템플릿의 {{PACE_BAND}} 와 같은 값)


# --------------------------------------------------------------------------- 계산
def quad(x: float, y: float) -> str:
    return ("t" if y >= 0 else "b") + ("r" if x >= 0 else "l")


def months_back(d: dt.date, n: int) -> dt.date:
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def close_on_or_before(s: pd.Series, d: dt.date) -> float | None:
    sub = s[s.index <= pd.Timestamp(d)]
    return float(sub.iloc[-1]) if len(sub) else None


def point(s: pd.Series, first: dt.date, snap: dt.date):
    """한 시점의 (1개월 수익률, 3개월 수익률). 비교일이 상장일보다 앞서면 None."""
    c0 = close_on_or_before(s, snap)
    if c0 is None:
        return (None, None)

    def ret(n: int):
        d = months_back(snap, n)
        if d < first:
            return None
        return round((c0 / close_on_or_before(s, d) - 1) * 100, 2)

    return (ret(1), ret(3))


def pace_of(x1: float, x3: float) -> float:
    """속도 변화 = 1개월 수익률 − 3개월 수익률÷3 (%p)."""
    return round(x1 - x3 / 3, 2)


QUAD_NAME = {"tr": "상승 지속", "tl": "하락 전환", "br": "상승 전환", "bl": "하락 지속"}
# 지난주 상승 쪽(상승 전환·상승 지속)이었다가 이번 주 하락 지속으로 내려온 종목은 속도 변화보다 이 표시를 우선한다
TURN_DOWN = {("br", "bl"): "반등 꺾임", ("tr", "bl"): "상승 꺾임"}


def pace_state(q: str, p: float, band: float = PACE_BAND):
    """상승 지속·하락 지속 안에서의 세부 상태 (키, 이름). 그 외 사분면은 None."""
    k = "acc" if p >= band else ("dec" if p <= -band else "flat")
    if q == "tr":
        return k, {"acc": "상승 가속", "flat": "상승 유지", "dec": "상승 둔화"}[k]
    if q == "bl":     # 하락 중에는 속도 변화가 +이면 낙폭이 줄어드는 중
        return k, {"acc": "낙폭 둔화", "flat": "하락 유지", "dec": "낙폭 확대"}[k]
    return None


def classify(pts) -> str:
    q = [quad(*p) if p[0] is not None and p[1] is not None else None for p in pts]
    if q[3] == "tr" and "br" in q[:3]:
        return "up"
    if q[3] == "bl" and "tl" in q[:3]:
        return "dn"
    return ""


def compute(close: pd.DataFrame, items: list[dict], asof: dt.date | None = None) -> dict:
    """items: [{kind, ticker, yf, name, label}]  ->  {"T":..., "snaps":[...], "items":[...]}"""
    dates = close.index
    if asof is not None:
        dates = dates[dates <= pd.Timestamp(asof)]
    T = dates[-1].date()
    snaps = [T - dt.timedelta(days=21), T - dt.timedelta(days=14), T - dt.timedelta(days=7), T]
    out = []
    for it in items:
        s = close[it["yf"]].dropna() if it["yf"] in close.columns else pd.Series(dtype=float)
        s = s[s.index <= pd.Timestamp(T)]
        rec = dict(it)
        if s.empty:
            rec.update(pts=None, missing=True, plotted=False, sig="")
            out.append(rec)
            continue
        first = s.index[0].date()
        pts = [point(s, first, sd) for sd in snaps]
        wk = close_on_or_before(s, T - dt.timedelta(days=7))
        w1 = round((float(s.iloc[-1]) / wk - 1) * 100, 2) if wk else None       # 최근 7일 수익률
        cur = pts[3]
        plotted = all(p[0] is not None and p[1] is not None for p in pts)   # 4개 시점이 모두 있어야 차트·신호 대상
        rec.update(
            pts=pts, missing=False, plotted=plotted, sig=classify(pts) if plotted else "", w1=w1,
            x1=cur[0], x3=cur[1], c=(round(cur[0] - cur[1], 2) if plotted else None),
            quad=quad(*cur) if plotted else None,
            pace=pace_of(*cur) if plotted else None,
            prev_quad=quad(*pts[2]) if (pts[2][0] is not None and pts[2][1] is not None) else None,
        )
        out.append(rec)
    return {"T": T, "snaps": snaps, "items": out}


# --------------------------------------------------------------------------- 시세
def fetch(symbols: list[str], tries: int = 3) -> pd.DataFrame:
    import yfinance as yf

    symbols = sorted(set(symbols))
    last_err: Exception | None = None
    for i in range(tries):
        try:
            raw = yf.download(symbols, period="9mo", interval="1d", auto_adjust=False,
                              progress=False, threads=False)
            close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].rename(columns={"Close": symbols[0]})
            close.index = pd.to_datetime(close.index).tz_localize(None)
            ok = [c for c in close.columns if close[c].notna().any()]
            if len(ok) >= 0.8 * len(symbols):
                return close.dropna(how="all")
            last_err = RuntimeError(f"시세를 받은 종목 {len(ok)}/{len(symbols)}")
        except Exception as e:  # noqa: BLE001
            last_err = e
        print(f"[fetch] 시도 {i + 1}/{tries} 실패: {last_err}", file=sys.stderr)
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"시세 수집 실패: {last_err}")


def drop_partial_day(close: pd.DataFrame, now: dt.datetime) -> pd.DataFrame:
    """장중에 돌렸을 때 오늘자(미완성) 행은 버린다. 미국 장 마감은 UTC 20~21시."""
    if close.index[-1].date() == now.date() and now.time() < dt.time(21, 30):
        return close.iloc[:-1]
    return close


# --------------------------------------------------------------------------- ETF 구성종목
HOLD_DAYS = 7           # 구성종목은 천천히 바뀌므로 일주일에 한 번만 새로 받는다
# 자주 나오는 종목의 한글 이름 (없으면 영문 이름을 줄여서 쓴다)
KO_NAMES = {
    "NVDA": "엔비디아", "MSFT": "마이크로소프트", "AAPL": "애플", "AMZN": "아마존", "GOOGL": "알파벳 A", "GOOG": "알파벳 C", "META": "메타",
    "AVGO": "브로드컴", "TSLA": "테슬라", "BRK-B": "버크셔해서웨이 B", "JPM": "JP모건", "LLY": "일라이릴리", "V": "비자", "MA": "마스터카드",
    "WMT": "월마트", "COST": "코스트코", "XOM": "엑슨모빌", "CVX": "셰브런", "UNH": "유나이티드헬스", "JNJ": "존슨앤존슨", "ABBV": "애브비",
    "ORCL": "오라클", "NFLX": "넷플릭스", "AMD": "AMD", "INTC": "인텔", "MU": "마이크론", "QCOM": "퀄컴", "TXN": "텍사스인스트루먼트",
    "AMAT": "어플라이드머티리얼즈", "LRCX": "램리서치", "KLAC": "KLA", "ASML": "ASML", "TSM": "TSMC", "ADI": "아날로그디바이스", "MRVL": "마벨",
    "CRM": "세일즈포스", "ADBE": "어도비", "NOW": "서비스나우", "INTU": "인튜이트", "PANW": "팰로앨토", "CRWD": "크라우드스트라이크", "PLTR": "팔란티어",
    "SNPS": "시놉시스", "CDNS": "케이던스", "FTNT": "포티넷", "ADSK": "오토데스크", "WDAY": "워크데이", "DDOG": "데이터독", "ZS": "지스케일러",
    "GE": "GE에어로스페이스", "RTX": "RTX", "LMT": "록히드마틴", "BA": "보잉", "NOC": "노스럽그루먼", "GD": "제너럴다이내믹스", "LHX": "L3해리스",
    "HII": "헌팅턴잉걸스", "KTOS": "크라토스", "HWM": "하우멧", "TDG": "트랜스다임", "AXON": "액손", "TXT": "텍스트론", "LDOS": "리도스",
    "CAT": "캐터필러", "DE": "존디어", "HON": "허니웰", "UNP": "유니온퍼시픽", "UPS": "UPS", "ETN": "이튼", "PH": "파커하니핀", "EMR": "에머슨",
    "LUV": "사우스웨스트항공", "DAL": "델타항공", "UAL": "유나이티드항공", "AAL": "아메리칸항공", "ALK": "알래스카항공", "JBLU": "제트블루",
    "NEE": "넥스트에라", "SO": "서던컴퍼니", "DUK": "듀크에너지", "CEG": "컨스텔레이션에너지", "VST": "비스트라", "AEP": "AEP", "D": "도미니언",
    "PG": "P&G", "KO": "코카콜라", "PEP": "펩시코", "PM": "필립모리스", "MO": "알트리아", "MDLZ": "몬델리즈", "CL": "콜게이트",
    "HD": "홈디포", "LOW": "로우스", "MCD": "맥도날드", "NKE": "나이키", "SBUX": "스타벅스", "BKNG": "부킹홀딩스", "TJX": "TJX",
    "BAC": "뱅크오브아메리카", "WFC": "웰스파고", "GS": "골드만삭스", "MS": "모건스탠리", "C": "씨티그룹", "AXP": "아메리칸익스프레스", "BLK": "블랙록",
    "SPGI": "S&P글로벌", "SCHW": "찰스슈왑", "PGR": "프로그레시브", "CB": "처브", "MMC": "마시앤드맥레넌",
    "MRK": "머크", "PFE": "화이자", "TMO": "써모피셔", "ABT": "애보트", "ISRG": "인튜이티브서지컬", "AMGN": "암젠", "GILD": "길리어드", "VRTX": "버텍스",
    "LIN": "린데", "SHW": "셔윈윌리엄스", "FCX": "프리포트맥모란", "NEM": "뉴몬트", "APD": "에어프로덕츠", "ECL": "에코랩", "NUE": "뉴코어", "STLD": "스틸다이내믹스",
    "RS": "리라이언스", "CLF": "클리블랜드클리프스", "AA": "알코아", "MP": "MP머티리얼즈", "HL": "헤클라마이닝", "CDE": "코이어마이닝", "RGLD": "로열골드",
    "T": "AT&T", "VZ": "버라이즌", "TMUS": "T모바일", "DIS": "디즈니", "CMCSA": "컴캐스트", "CHTR": "차터", "EA": "EA", "TTWO": "테이크투", "WBD": "워너브라더스디스커버리",
    "NBIS": "네비우스", "CRWV": "코어위브", "IREN": "아이렌", "LITE": "루멘텀", "COHR": "코히런트", "CIEN": "시에나", "ANET": "아리스타", "CSCO": "시스코",
    "MTRN": "머티리온", "VRT": "버티브", "PWR": "콴타서비스", "GEV": "GE버노바", "SPCX": "스페이스X", "RKLB": "로켓랩", "IONQ": "아이온큐",
    "WELL": "웰타워", "PLD": "프롤로지스", "AMT": "아메리칸타워", "EQIX": "에퀴닉스", "O": "리얼티인컴", "SPG": "사이먼프로퍼티",
    "COP": "코노코필립스", "EOG": "EOG리소시스", "KMI": "킨더모건", "SLB": "SLB", "UBER": "우버", "SNOW": "스노우플레이크", "NET": "클라우드플레어",
    "TGT": "타깃", "MAR": "메리어트", "DLR": "디지털리얼티", "PSA": "퍼블릭스토리지", "VLO": "발레로에너지", "MPC": "마라톤페트롤리움", "PSX": "필립스66",
    "APP": "앱러빈", "GRMN": "가민", "HPE": "HPE", "STX": "씨게이트", "ROST": "로스스토어스", "TRV": "트래블러스", "WMB": "윌리엄스", "XEL": "엑셀에너지",
    "SRE": "셈프라", "ETR": "엔터지", "MLM": "마틴마리에타", "VMC": "벌컨머티리얼스", "CRH": "CRH", "CMC": "커머셜메탈스", "BE": "블룸에너지", "NVT": "엔벤트",
    "AC.TO": "에어캐나다", "CCO.TO": "카메코", "ASTS": "AST스페이스모바일", "PL": "플래닛랩스", "IRDM": "이리듐", "VSAT": "비아샛", "HUT": "헛8", "OKTA": "옥타",
    "ESTC": "엘라스틱", "NTNX": "뉴타닉스", "TWLO": "트윌리오", "TRMB": "트림블", "ENTG": "엔테그리스", "FTI": "TechnipFMC", "UTHR": "유나이티드테라퓨틱스",
    "THC": "테넷헬스케어", "XPO": "XPO", "SKYW": "스카이웨스트", "ALGT": "얼리전트", "ULCC": "프론티어항공", "SIRI": "시리우스XM", "ATI": "ATI", "MOG-A": "무그",
    "005930.KS": "삼성전자", "000660.KS": "SK하이닉스", "005930.KQ": "삼성전자", "000660.KQ": "SK하이닉스", "3443.TW": "글로벌유니칩", "2330.TW": "TSMC",
}
_NAME_CUT = (" Class A", " Class B", " Class C", " Shs", " Inc.", " Inc", " Corp.", " Corp", " Co.", " Co", " Ltd.", " Ltd", " PLC", " plc", " N.V.", " NV", " Holdings", " Group", " Company", ",")


def short_en(name: str) -> str:
    n = str(name)
    for c in _NAME_CUT:
        i = n.find(c)
        if i > 2:
            n = n[:i]
    return n.strip()[:22]


def fetch_one_holdings(ticker: str) -> list[dict]:
    import yfinance as yf

    th = yf.Ticker(ticker).funds_data.top_holdings
    if th is None or len(th) == 0:
        return []
    rows = []
    for sym, r in th.iterrows():
        pct = float(r["Holding Percent"]) * 100
        if pct <= 0:
            continue
        sym = str(sym)
        rows.append({"s": sym, "n": KO_NAMES.get(sym) or short_en(r["Name"]), "w": round(pct, 2)})
    return rows


def update_holdings(cfg: dict, prev: dict | None, today: dt.date) -> dict:
    """섹터 ETF 별 상위 구성종목. 일주일이 안 지났고 목록에 있는 ETF 는 이전 값을 그대로 쓴다. 한 ETF 를 못 받아도 나머지는 계속."""
    old = (prev or {}).get("holdings") or {}
    old_items = old.get("items", {})
    try:
        age = (today - dt.date.fromisoformat(old.get("updated", "2000-01-01"))).days
    except ValueError:
        age = 999
    fresh = age < HOLD_DAYS and os.environ.get("HOLD_FORCE") != "1"
    items, refreshed = {}, 0
    for w in cfg.get("sectors", []):
        t = w["ticker"].strip().upper()
        if fresh and t in old_items:
            items[t] = old_items[t]
            continue
        try:
            rows = fetch_one_holdings(t)
            items[t] = {"rows": rows} if rows else {"rows": [], "note": "Holdings 정보가 없는 ETF입니다 (선물 등)"}
            refreshed += 1
        except Exception as e:  # noqa: BLE001
            print(f"[holdings] {t} 실패: {type(e).__name__}", file=sys.stderr)
            if t in old_items:
                items[t] = old_items[t]
        time.sleep(0.25)
    return {"updated": f"{today}" if (refreshed or not old) else old.get("updated", f"{today}"), "items": items}


# --------------------------------------------------------------------------- 앱 데이터와 PC 화면
def build_app_data(res: dict) -> dict:
    """스마트폰 앱(PWA)과 PC 화면이 함께 쓰는 계산 결과. 판정 로직은 이 파일에만 두고 화면은 보여주기만 한다.
    items/missing = 관심종목, sectors = {items, missing} = 섹터 ETF 목록."""
    kst = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)

    def pack(kind: str) -> dict:
        items, mine = [], [it for it in res["items"] if it["kind"] == kind]
        for it in mine:
            if it["missing"] or it["pts"][3][0] is None:
                continue
            rec = {"t": it["ticker"], "n": it["name"], "g": it["label"], "w1": it.get("w1")}
            if it.get("style"):
                rec["st"] = it["style"]
            if it["plotted"]:
                st = pace_state(it["quad"], it["pace"])
                rec.update(h=[[p[0], p[1]] for p in it["pts"]], x1=it["x1"], x3=it["x3"], c=it["c"], pace=it["pace"],
                           quad=it["quad"], prev=it["prev_quad"], sig=it["sig"],
                           state=st[1] if st else "", state_key=st[0] if st else "",
                           turn=TURN_DOWN.get((it["prev_quad"], it["quad"]), ""))
            else:
                rec.update(h=None, x1m=it["pts"][3][0])       # 상장 3개월 미만: 목록에만 표시
            items.append(rec)
        shown = {r["t"] for r in items}
        missing = [it["ticker"] for it in mine if it["ticker"] not in shown]      # 시세가 없거나 상장 1개월 미만: 앱이 '계산 중'과 구분해 보여준다
        return {"items": items, "missing": missing}

    w, sec = pack("stock"), pack("etf")
    return {"asof": f"{res['T']}", "snaps": [f"{d}" for d in res["snaps"]],
            "updated": f"{kst:%Y-%m-%d %H:%M} KST", "band": PACE_BAND, "quad_names": QUAD_NAME,
            "items": w["items"], "missing": w["missing"], "sectors": sec}


def export_app(data: dict, path: Path) -> None:
    """data.json: 스마트폰 앱이 GitHub Pages 에서 읽는 파일."""
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def render_page(data: dict, cfg: dict) -> str:
    """PC 화면: 스마트폰 앱(index.html)과 같은 화면에 이번 계산 결과와 관심종목을 넣은 HTML 한 장.
    파일로 바로 열려야 하므로 데이터·설정을 안에 넣고, 서비스 워커는 쓰지 않는다."""
    s = (HERE / "index.html").read_text(encoding="utf-8")

    def sub(old: str, new: str) -> None:
        nonlocal s
        if old not in s:
            raise RuntimeError(f"앱 화면(index.html)에서 바꿀 자리를 찾지 못했습니다: {old[:50]}")
        s = s.replace(old, new, 1)

    sub("""    const r = await fetch('data.json?_=' + Date.now(), {cache: 'no-store'});
    if (!r.ok) throw new Error(r.status);
    D = await r.json();""", "    D = INLINE;")
    sub("""    const r = await fetch('config.json?_=' + Date.now(), {cache: 'no-store'});
    if (!r.ok) throw new Error(r.status);
    CFG = await r.json();""", "    CFG = JSON.parse(JSON.stringify(INLINE_CFG));")
    sub("if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});", "")
    sub("const QN =", "const INLINE = " + json.dumps(data, ensure_ascii=False) + ";\nconst INLINE_CFG = " + json.dumps(cfg, ensure_ascii=False) + ";\nconst QN =")
    return s


# --------------------------------------------------------------------------- 실행
def load_items(cfg: dict) -> list[dict]:
    """관심종목(kind=stock)과 섹터 ETF(kind=etf)를 한 목록으로. 같은 티커가 두 목록에 있어도 각각 계산한다(시세는 한 번만 받음)."""
    items = []
    for kind, key, label in (("stock", "watchlist", cfg.get("group", "관심종목")), ("etf", "sectors", "섹터")):
        seen = set()
        for w in cfg.get(key, []):
            sym = w["ticker"].strip().upper().replace(".", "-")      # BRK.B -> BRK-B (yfinance 표기)
            if sym in seen:
                continue
            seen.add(sym)
            items.append({"kind": kind, "ticker": w["ticker"].strip().upper(), "yf": sym,
                          "name": w.get("name") or w["ticker"], "label": w.get("group") or label, "style": w.get("style", "")})
    return items


def summary(res: dict) -> str:
    names = {"tr": "상승 지속", "br": "상승 전환", "tl": "하락 전환", "bl": "하락 지속"}
    lines = [f"기준일 {res['T']}"]
    for i in res["items"]:
        if i["missing"]:
            lines.append(f"  {i['ticker']:<6} 시세 없음")
        elif not i["plotted"]:
            lines.append(f"  {i['ticker']:<6} {i['name']}  1개월 {i['pts'][3][0]}%  (상장 3개월 미만: 표에만 표시)")
        else:
            sig = {"up": " ▲", "dn": " ▼"}.get(i["sig"], "")
            lines.append(f"  {i['ticker']:<6} {i['name']}  1M {i['x1']:+.1f}%  3M {i['x3']:+.1f}%  {names[i['quad']]}{sig}")
    return "\n".join(lines)


def run(args) -> int:
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    items = load_items(cfg)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    asof = dt.date.fromisoformat(args.asof) if args.asof else None

    close = fetch([i["yf"] for i in items])
    if asof is None:
        close = drop_partial_day(close, now)
    res = compute(close, items, asof)
    if asof is None and (now.date() - res["T"]).days > 6:
        raise RuntimeError(f"최신 시세가 너무 오래됐습니다(마지막 거래일 {res['T']}). 인터넷 연결을 확인하세요.")

    OUT.mkdir(exist_ok=True)
    page_path = OUT / f"watchlist_{res['T']:%Y-%m-%d}.html"
    data = build_app_data(res)
    try:
        prev = json.loads((HERE / "data.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = None
    try:
        data["holdings"] = update_holdings(cfg, prev, now.date())
    except Exception as e:  # noqa: BLE001
        print(f"[holdings] 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        if prev and prev.get("holdings"):
            data["holdings"] = prev["holdings"]
    page_path.write_text(render_page(data, cfg), encoding="utf-8")
    latest = HERE / "관심종목_모멘텀_최신.html"
    latest.write_text(page_path.read_text(encoding="utf-8"), encoding="utf-8")

    if not args.asof:                   # 과거 기준일 재계산은 앱 데이터를 덮어쓰지 않는다
        export_app(data, HERE / "data.json")

    if args.dump_json:
        slim = [{k: v for k, v in i.items() if k in ("kind", "ticker", "pts", "plotted", "sig", "x1", "x3", "c", "quad", "pace")} for i in res["items"]]
        Path(args.dump_json).write_text(json.dumps({"T": str(res["T"]), "items": slim}, ensure_ascii=False), encoding="utf-8")

    print(summary(res))
    print(f"\n[page] {latest}")
    if not args.no_open:
        os.startfile(latest)            # Windows: 기본 브라우저로 열기
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="관심종목 모멘텀 추적기")
    ap.add_argument("--no-open", action="store_true", help="HTML 만 만들고 브라우저로 열지 않음")
    ap.add_argument("--asof", help="기준일(YYYY-MM-DD). 이 날짜 이전 마지막 거래일로 계산")
    ap.add_argument("--dump-json", help="계산 결과를 JSON 으로 저장(검증용)")
    args = ap.parse_args()
    try:
        return run(args)
    except Exception as e:  # noqa: BLE001
        print(f"[오류] {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
