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
        cur = pts[3]
        plotted = all(p[0] is not None and p[1] is not None for p in pts)   # 4개 시점이 모두 있어야 차트·신호 대상
        rec.update(
            pts=pts, missing=False, plotted=plotted, sig=classify(pts) if plotted else "",
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


# --------------------------------------------------------------------------- 인터랙티브 페이지
def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def render_page(res: dict, cfg: dict) -> str:
    """page_template.html 에 데이터를 채워 한 개의 HTML 파일로 만든다 (오프라인에서도 동작, 글꼴만 온라인 로드)."""
    tpl = (HERE / "page_template.html").read_text(encoding="utf-8")
    data = {"stocks": []}
    for it in res["items"]:
        if it["missing"]:
            continue
        base = {"t": it["ticker"], "n": it["name"], "s": it["label"]}
        if it["plotted"]:
            base["h"] = [[p[0], p[1]] for p in it["pts"]]
        elif it["pts"][3][0] is not None:
            base.update(h=None, x1m=it["pts"][3][0])      # 표에만 표시
        else:
            continue                                      # 상장 1개월 미만: 표시할 값이 없음
        data["stocks"].append(base)
    T, sn = res["T"], res["snaps"]
    tickers = ", ".join(f"{w['name']} {w['ticker']}" for w in cfg["watchlist"])
    repl = {
        "{{DATA}}": json.dumps(data, ensure_ascii=False),
        "{{ASOF}}": f"{T:%Y-%m-%d}", "{{M1}}": f"{months_back(T, 1):%Y-%m-%d}", "{{M3}}": f"{months_back(T, 3):%Y-%m-%d}",
        "{{S0}}": f"{sn[0]:%m-%d}", "{{S1}}": f"{sn[1]:%m-%d}", "{{S2}}": f"{sn[2]:%m-%d}", "{{S3}}": f"{sn[3]:%m-%d}",
        "{{PACE_BAND}}": f"{PACE_BAND:g}", "{{N_STOCK}}": str(len(data["stocks"])),
        "{{NOTE_STOCKS}}": esc(cfg.get("page_note", "")), "{{TICKER_LIST}}": esc(tickers),
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    left = re.findall(r"\{\{[A-Z0-9_]+\}\}", tpl)
    if left:
        raise RuntimeError(f"페이지 템플릿에 채워지지 않은 항목: {sorted(set(left))}")
    return tpl


# --------------------------------------------------------------------------- 스마트폰 앱용 데이터
def export_app(res: dict, cfg: dict, path: Path) -> None:
    """data.json: 스마트폰 앱(PWA)이 읽는 계산 결과. 판정 로직은 이 파일에만 두고 앱은 보여주기만 한다."""
    kst = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)
    items = []
    for it in res["items"]:
        if it["missing"] or it["pts"][3][0] is None:
            continue
        rec = {"t": it["ticker"], "n": it["name"], "g": it["label"]}
        if it["plotted"]:
            st = pace_state(it["quad"], it["pace"])
            rec.update(h=[[p[0], p[1]] for p in it["pts"]], x1=it["x1"], x3=it["x3"], c=it["c"], pace=it["pace"],
                       quad=it["quad"], prev=it["prev_quad"], sig=it["sig"],
                       state=st[1] if st else "", state_key=st[0] if st else "",
                       turn=TURN_DOWN.get((it["prev_quad"], it["quad"]), ""))
        else:
            rec.update(h=None, x1m=it["pts"][3][0])       # 상장 3개월 미만: 목록에만 표시
        items.append(rec)
    data = {"asof": f"{res['T']}", "snaps": [f"{d}" for d in res["snaps"]],
            "updated": f"{kst:%Y-%m-%d %H:%M} KST", "band": PACE_BAND, "quad_names": QUAD_NAME, "items": items}
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# --------------------------------------------------------------------------- 실행
def load_items(cfg: dict) -> list[dict]:
    seen, items = set(), []
    for w in cfg["watchlist"]:
        sym = w["ticker"].strip().upper().replace(".", "-")      # BRK.B -> BRK-B (yfinance 표기)
        if sym in seen:
            continue
        seen.add(sym)
        items.append({"kind": "stock", "ticker": w["ticker"].strip().upper(), "yf": sym,
                      "name": w.get("name") or w["ticker"], "label": w.get("group") or cfg.get("group", "관심종목")})
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
    page_path.write_text(render_page(res, cfg), encoding="utf-8")
    latest = HERE / "관심종목_모멘텀_최신.html"
    latest.write_text(page_path.read_text(encoding="utf-8"), encoding="utf-8")

    if not args.asof:                   # 과거 기준일 재계산은 앱 데이터를 덮어쓰지 않는다
        export_app(res, cfg, HERE / "data.json")

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
