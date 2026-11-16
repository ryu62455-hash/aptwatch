#!/usr/bin/env python3
"""실거래 리포트 자동 생성기 (표준 라이브러리만 사용).

국토교통부 아파트 매매 실거래가 공공 API 데이터를 받아
지역별 거래 동향, 최고가 경신 단지, 단지별 거래 이력 페이지를 정적 HTML로 만든다.

사용법:
  python build.py --check     # 인증키와 API 응답 형식을 한 번 확인
  python build.py             # 실제 데이터로 public/ 폴더 생성
  python build.py --demo      # 샘플 데이터로 preview/ 폴더 생성 (공개 금지)
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

ENDPOINT = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
HERE = os.path.dirname(os.path.abspath(__file__))
PYEONG = 3.3058  # 1평 = 3.3058㎡


# ---------------------------------------------------------------- 설정/유틸
def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def month_list(n, today):
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_price(man):
    """만원 단위 정수를 '12억 3,000만원' 형태로."""
    man = int(round(man))
    eok, rest = divmod(man, 10000)
    if eok and rest:
        return f"{eok}억 {rest:,}만원"
    if eok:
        return f"{eok}억원"
    return f"{rest:,}만원"


def fmt_date(d):
    return f"{d.year}년 {d.month}월 {d.day}일"


def slug(*parts):
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def to_int(s, default=0):
    try:
        return int(str(s).replace(",", "").strip())
    except ValueError:
        return default


def to_float(s, default=0.0):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return default


# ---------------------------------------------------------------- 데이터 수집
class ApiError(Exception):
    pass


def api_request(key, code, ym, page, rows=1000):
    q = urllib.parse.urlencode(
        {"serviceKey": urllib.parse.unquote(key), "LAWD_CD": code, "DEAL_YMD": ym,
         "pageNo": page, "numOfRows": rows}
    )
    req = urllib.request.Request(f"{ENDPOINT}?{q}", headers={"User-Agent": "aptwatch/1.0"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:  # 네트워크 오류는 재시도
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise ApiError(f"요청 실패: {last}")


def parse_response(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ApiError("XML 형식이 아닌 응답: " + raw[:200].decode("utf-8", "replace"))
    code = (root.findtext(".//resultCode") or "").strip()
    if code and code not in ("00", "000"):
        msg = (root.findtext(".//resultMsg") or "").strip()
        if "NO_DATA" in msg.upper() or code == "03":
            return [], 0
        raise ApiError(f"API 오류 {code}: {msg}")
    total = to_int(root.findtext(".//totalCount") or "0")
    items = []
    for it in root.iter("item"):
        items.append({c.tag: (c.text or "").strip() for c in it})
    return items, total


def fetch_month(key, code, ym):
    items, page = [], 1
    while True:
        raw = api_request(key, code, ym, page)
        got, total = parse_response(raw)
        items.extend(got)
        if not got or len(items) >= total:
            break
        page += 1
        time.sleep(0.15)
    return items


def pick(d, *names):
    for n in names:
        if n in d and d[n] != "":
            return d[n]
    return ""


def to_records(items):
    recs = []
    for it in items:
        if pick(it, "cdealType", "cdealDay"):  # 계약 해제 건 제외
            continue
        apt = pick(it, "aptNm", "apt_nm")
        price = to_int(pick(it, "dealAmount", "deal_amount"))
        area = to_float(pick(it, "excluUseAr", "exclu_use_ar"))
        try:
            d = dt.date(to_int(pick(it, "dealYear")), to_int(pick(it, "dealMonth")), to_int(pick(it, "dealDay")))
        except ValueError:
            continue
        if not apt or price <= 0 or area <= 0:
            continue
        recs.append({
            "apt": apt, "dong": pick(it, "umdNm", "umd_nm") or "-", "area": int(round(area)),
            "price": price, "floor": to_int(pick(it, "floor")), "date": d,
            "built": to_int(pick(it, "buildYear")),
        })
    return recs


def collect_live(cfg, today):
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("환경변수 DATA_GO_KR_KEY 가 없습니다. README의 1단계를 확인하세요.")
    cache_dir = os.path.join(HERE, "data", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    months = month_list(cfg["months_back"], today)
    fresh = set(months[:2])  # 이번 달과 지난달은 항상 새로 받는다 (신고 지연 반영)
    result, failures, ok = {}, 0, 0
    for reg in cfg["regions"]:
        recs = []
        for ym in months:
            path = os.path.join(cache_dir, f"{reg['code']}-{ym}.json")
            items = None
            if ym not in fresh and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    items = json.load(f)
            else:
                try:
                    items = fetch_month(key, reg["code"], ym)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(items, f, ensure_ascii=False)
                    ok += 1
                    time.sleep(0.2)
                except ApiError as e:
                    failures += 1
                    print(f"[경고] {reg['name']} {ym}: {e}", file=sys.stderr)
                    if os.path.exists(path):
                        with open(path, encoding="utf-8") as f:
                            items = json.load(f)
            if items:
                recs.extend(to_records(items))
        result[reg["code"]] = recs
        print(f"{reg['name']}: {len(recs)}건")
    if ok == 0 and failures > 0:
        sys.exit("모든 API 호출이 실패했습니다. 인증키와 활용신청 상태를 확인하세요.")
    return result


def collect_demo(cfg, today):
    rnd = random.Random(7)
    names = ["샘플e편한", "샘플푸르지오", "샘플자이", "샘플롯데캐슬", "샘플래미안", "샘플힐스테이트", "샘플아이파크", "샘플센트럴"]
    dongs = ["가나동", "다라동", "마바동"]
    result = {}
    for ri, reg in enumerate(cfg["regions"][:2]):
        base_m2 = 1500 + ri * 700
        recs = []
        for ci, nm in enumerate(names):
            dong, built = dongs[ci % 3], 1995 + ci * 3
            for area in (59, 84, 114)[: 2 + ci % 2]:
                level = base_m2 * area * (0.8 + 0.06 * ci) / 10
                for _ in range(rnd.randint(5, 14)):
                    days_ago = rnd.randint(0, 29 * cfg["months_back"])
                    trend = 1 + (180 - days_ago) / 180 * 0.06
                    price = int(level * trend * rnd.uniform(0.96, 1.05) / 100) * 100
                    recs.append({"apt": nm, "dong": dong, "area": area, "price": price,
                                 "floor": rnd.randint(2, 25), "date": today - dt.timedelta(days=days_ago), "built": built})
        result[reg["code"]] = recs
    return result


# ---------------------------------------------------------------- 분석
def analyze(region, recs, cfg, today):
    window = dt.timedelta(days=cfg["new_high_window_days"])
    complexes = defaultdict(list)
    for r in recs:
        complexes[(r["dong"], r["apt"])].append(r)

    cinfo, highs = [], []
    for (dong, apt), trades in complexes.items():
        trades.sort(key=lambda r: (r["date"], r["price"]))
        areas = defaultdict(list)
        for t in trades:
            areas[t["area"]].append(t)
        for area, ts in areas.items():
            for idx in range(len(ts) - 1, 0, -1):
                t = ts[idx]
                if t["date"] < today - window:
                    break
                prev_max = max(p["price"] for p in ts[:idx])
                if t["price"] > prev_max:
                    highs.append({"dong": dong, "apt": apt, "area": area, "price": t["price"], "prev": prev_max,
                                  "date": t["date"], "floor": t["floor"], "cid": slug(region["code"], dong, apt)})
                    break
        cinfo.append({"id": slug(region["code"], dong, apt), "dong": dong, "apt": apt,
                      "built": next((t["built"] for t in trades if t["built"]), 0),
                      "trades": trades, "areas": dict(areas)})
    highs.sort(key=lambda h: (h["price"] - h["prev"]) / h["prev"], reverse=True)

    monthly = defaultdict(list)
    for r in recs:
        monthly[f"{r['date'].year}{r['date'].month:02d}"].append(r)
    mstats = {}
    for ym, rs in monthly.items():
        per_py = [r["price"] / (r["area"] / PYEONG) for r in rs]
        mstats[ym] = {"count": len(rs), "median_py": statistics.median(per_py)}
    return {"region": region, "complexes": cinfo, "highs": highs, "monthly": mstats,
            "recent": sorted(recs, key=lambda r: r["date"], reverse=True)[:20], "total": len(recs)}


# ---------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f4f6f9;--sf:#fff;--ink:#14202b;--mu:#5d6b79;--ln:#dde3ea;--ac:#1f4e9e;--up:#d23a2e;--dn:#2a6fd0}
@media(prefers-color-scheme:dark){:root{--bg:#0f151c;--sf:#172029;--ink:#e8eef4;--mu:#97a5b3;--ln:#27333f;--ac:#7aa5ee;--up:#ff6b5e;--dn:#6fa2f0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
a{color:var(--ac)}
.w{max-width:920px;margin:0 auto;padding:0 16px}
header.top{background:var(--sf);border-bottom:1px solid var(--ln)}
header.top .w{display:flex;justify-content:space-between;align-items:center;padding-top:14px;padding-bottom:14px;gap:12px;flex-wrap:wrap}
header.top a.brand{font-weight:800;font-size:18px;color:var(--ink);text-decoration:none}
nav a{margin-left:14px;color:var(--mu);text-decoration:none;font-size:14px}
main{padding:24px 0 48px;display:flex;flex-direction:column;gap:18px}
h1{font-size:clamp(22px,5vw,30px);line-height:1.3;margin:0;letter-spacing:-.01em;text-wrap:balance}
h2{font-size:18px;margin:0 0 10px}
.card{background:var(--sf);border:1px solid var(--ln);border-radius:8px;padding:18px;min-width:0}
.mu{color:var(--mu);font-size:13px}
.sc{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}
th,td{padding:8px 6px;border-bottom:1px solid var(--ln);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal}
th{color:var(--mu);font-weight:500;font-size:12px}
.up{color:var(--up);font-weight:700}.dn{color:var(--dn);font-weight:700}
.bars{display:flex;flex-direction:column;gap:6px}
.bar{display:grid;grid-template-columns:64px 1fr 60px;gap:8px;align-items:center;font-size:13px}
.bar i{display:block;height:14px;background:var(--ac);border-radius:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.spark{width:100%;height:auto;max-width:360px}
.spark polyline{fill:none;stroke:var(--ac);stroke-width:2}.spark circle{fill:var(--ac)}.spark text{fill:var(--mu);font-size:10px}
.banner{background:#fff3cd;color:#5c4400;border:1px solid #e6cf7a;border-radius:6px;padding:10px 14px;font-weight:700}
footer{border-top:1px solid var(--ln);padding:20px 0 40px;color:var(--mu);font-size:12.5px}
"""


class Site:
    def __init__(self, cfg, outdir, demo):
        self.cfg, self.out, self.demo = cfg, outdir, demo
        self.pages = []  # (path, lastmod)
        self.today = dt.date.today()

    def write(self, rel, content, index=True):
        path = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if index and rel.endswith(".html"):
            self.pages.append(rel)

    def ad(self):
        c, s = self.cfg.get("adsense_client", ""), self.cfg.get("adsense_slot", "")
        if self.demo or not c or not s:
            return ""
        return (f'<div class="card"><ins class="adsbygoogle" style="display:block" data-ad-client="{esc(c)}" '
                f'data-ad-slot="{esc(s)}" data-ad-format="auto" data-full-width-responsive="true"></ins>'
                '<script>(adsbygoogle=window.adsbygoogle||[]).push({});</script></div>')

    def page(self, rel, title, desc, body, crumbs=None, index=True):
        cfg = self.cfg
        url = cfg["site_url"].rstrip("/") + "/" + ("" if rel == "index.html" else rel)
        full_title = f"{title} | {cfg['site_name']}" if rel != "index.html" else title
        robots = '<meta name="robots" content="noindex,nofollow">' if (self.demo or not index) else ""
        ads = ""
        if cfg.get("adsense_client") and not self.demo:
            ads = f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={esc(cfg["adsense_client"])}" crossorigin="anonymous"></script>'
        ld = ""
        if crumbs:
            items = [{"@type": "ListItem", "position": i + 1, "name": n,
                      "item": cfg["site_url"].rstrip("/") + "/" + (p if p != "index.html" else "")} for i, (n, p) in enumerate(crumbs)]
            ld = '<script type="application/ld+json">' + json.dumps(
                {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}, ensure_ascii=False) + "</script>"
        banner = '<div class="banner">샘플 데이터입니다. 실제 거래가 아니며 공개하면 안 됩니다.</div>' if self.demo else ""
        regions = "".join(f'<a href="r-{r["code"]}.html">{esc(r["name"])}</a>' for r in cfg["regions"][:3])
        doc = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(full_title)}</title><meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(url)}">{robots}<link rel="stylesheet" href="style.css">{ads}{ld}</head>
<body><header class="top"><div class="w"><a class="brand" href="index.html">{esc(cfg['site_name'])}</a><nav>{regions}<a href="about.html">소개</a></nav></div></header>
<div class="w"><main>{banner}{body}</main>
<footer>데이터 출처: 국토교통부 아파트 매매 실거래가(공공데이터포털). 계약 후 최대 30일 안에 신고되므로 최근 거래는 아직 반영되지 않았을 수 있습니다.
이 사이트의 내용은 참고용이며 투자나 거래를 권유하지 않습니다. 최종 확인은 국토교통부 실거래가 공개시스템에서 하세요.
최종 갱신 {fmt_date(self.today)} · <a href="about.html">소개</a> · <a href="privacy.html">개인정보처리방침</a></footer></div></body></html>"""
        self.write(rel, doc, index)


def sparkline(trades):
    if len(trades) < 3:
        return ""
    w, h, pad = 340, 90, 14
    xs = [t["date"].toordinal() for t in trades]
    ys = [t["price"] for t in trades]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx = lambda x: pad + (x - x0) / max(1, x1 - x0) * (w - 2 * pad)
    sy = lambda y: h - pad - (y - y0) / max(1, y1 - y0) * (h - 2 * pad)
    pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(xs, ys))
    dots = "".join(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.5"/>' for x, y in zip(xs, ys))
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" aria-label="거래가 추이">'
            f'<polyline points="{pts}"/>{dots}'
            f'<text x="{pad}" y="10">{esc(fmt_price(y1))}</text><text x="{pad}" y="{h - 2}">{esc(fmt_price(y0))}</text></svg>')


def build_site(cfg, data, outdir, demo):
    site = Site(cfg, outdir, demo)
    today = site.today
    site.write("style.css", CSS, index=False)
    site.write(".nojekyll", "", index=False)

    analyses = []
    for reg in cfg["regions"]:
        if reg["code"] in data and data[reg["code"]]:
            analyses.append(analyze(reg, data[reg["code"]], cfg, today))

    for a in analyses:
        reg = a["region"]
        months = sorted(a["monthly"].keys(), reverse=True)
        cur = months[0] if months else None
        prev = months[1] if len(months) > 1 else None
        cs = a["monthly"].get(cur, {"count": 0, "median_py": 0})
        ps = a["monthly"].get(prev, {"count": 0, "median_py": 0}) if prev else {"count": 0, "median_py": 0}
        change = (cs["median_py"] / ps["median_py"] - 1) * 100 if ps["median_py"] else 0
        cls = "up" if change > 0 else "dn"
        arrow = "▲" if change > 0 else "▼"
        story = (f"{reg['name']} 아파트 매매는 {int(cur[:4])}년 {int(cur[4:])}월에 지금까지 {cs['count']}건 신고됐습니다. "
                 f"평당 중위가격은 {cs['median_py']:,.0f}만원입니다." if cur else "")
        if prev and ps["median_py"]:
            story += f" 직전 달({int(prev[:4])}년 {int(prev[4:])}월, {ps['count']}건) 대비 평당 중위가격은 {abs(change):.1f}% {'올랐' if change > 0 else '내렸'}습니다."
        story += f" 최근 {cfg['new_high_window_days']}일 안에 조회 기간 최고가를 넘긴 단지·면적은 {len(a['highs'])}곳입니다."

        top_vol = sorted(a["complexes"], key=lambda c: len(c["trades"]), reverse=True)[:10]
        maxc = max([a["monthly"][m]["count"] for m in months] + [1])
        bars = "".join(f'<div class="bar"><span>{m[:4]}.{m[4:]}</span><i style="width:{a["monthly"][m]["count"] / maxc * 100:.0f}%"></i><span>{a["monthly"][m]["count"]}건</span></div>'
                       for m in sorted(months))
        high_rows = "".join(
            f'<tr><td><a href="c-{reg["code"]}-{h["cid"]}.html">{esc(h["apt"])}</a><div class="mu">{esc(h["dong"])}</div></td><td>{h["area"]}㎡</td>'
            f'<td>{esc(fmt_price(h["price"]))}</td><td class="up">+{(h["price"] / h["prev"] - 1) * 100:.1f}%</td><td>{fmt_date(h["date"])}</td></tr>'
            for h in a["highs"][:15]) or '<tr><td colspan="5" class="mu">해당 기간에 최고가를 넘긴 거래가 없습니다.</td></tr>'
        vol_rows = "".join(
            f'<tr><td><a href="c-{reg["code"]}-{c["id"]}.html">{esc(c["apt"])}</a><div class="mu">{esc(c["dong"])}</div></td><td>{len(c["trades"])}건</td>'
            f'<td>{esc(fmt_price(c["trades"][-1]["price"]))}</td></tr>' for c in top_vol)
        recent_rows = "".join(
            f'<tr><td>{esc(r["apt"])}<div class="mu">{esc(r["dong"])}</div></td><td>{r["area"]}㎡</td><td>{r["floor"]}층</td>'
            f'<td>{esc(fmt_price(r["price"]))}</td><td>{fmt_date(r["date"])}</td></tr>' for r in a["recent"])
        body = f"""<h1>{esc(reg['name'])} 아파트 실거래가 동향</h1>
<div class="card"><p style="margin:0">{esc(story)}</p><p class="mu" style="margin:8px 0 0">평당 중위가격 변동 <span class="{cls}">{arrow} {abs(change):.1f}%</span> (전월 대비, 이번 달은 신고가 끝나지 않아 달라질 수 있음)</p></div>
{site.ad()}
<div class="card"><h2>월별 거래량</h2><div class="bars">{bars}</div></div>
<div class="card"><h2>최근 {cfg['new_high_window_days']}일 최고가 경신</h2>
<p class="mu">같은 단지, 같은 전용면적에서 조회 기간 최고가를 넘긴 거래입니다.</p>
<div class="sc"><table><thead><tr><th>단지</th><th>면적</th><th>거래가</th><th>직전 최고가 대비</th><th>계약일</th></tr></thead><tbody>{high_rows}</tbody></table></div></div>
<div class="card"><h2>거래가 많은 단지</h2><div class="sc"><table><thead><tr><th>단지</th><th>거래 건수</th><th>가장 최근 거래가</th></tr></thead><tbody>{vol_rows}</tbody></table></div></div>
<div class="card"><h2>최근 신고된 거래</h2><div class="sc"><table><thead><tr><th>단지</th><th>면적</th><th>층</th><th>거래가</th><th>계약일</th></tr></thead><tbody>{recent_rows}</tbody></table></div></div>"""
        site.page(f"r-{reg['code']}.html", f"{reg['name']} 아파트 실거래가 최고가 경신 단지",
                  f"{reg['name']} 아파트 월별 거래량, 평당가 변동, 최근 {cfg['new_high_window_days']}일 최고가 경신 단지를 매일 갱신합니다.",
                  body, crumbs=[(cfg["site_name"], "index.html"), (reg["name"], f"r-{reg['code']}.html")])

        # 단지 페이지 (거래가 적은 단지는 내용이 빈약하므로 제외)
        cpages = [c for c in a["complexes"] if len(c["trades"]) >= 2]
        cpages.sort(key=lambda c: len(c["trades"]), reverse=True)
        for c in cpages[: cfg["max_complex_pages_per_region"]]:
            sections, sentences = "", []
            for area in sorted(c["areas"]):
                ts = c["areas"][area]
                last, best = ts[-1], max(ts, key=lambda t: t["price"])
                gap = (last["price"] / best["price"] - 1) * 100
                if gap < -0.05:
                    tail = f"조회 기간 최고가({fmt_price(best['price'])}, {fmt_date(best['date'])})보다 {abs(gap):.1f}% 낮습니다."
                else:
                    tail = "조회 기간 중 가장 높은 가격입니다."
                sentences.append(f"전용 {area}㎡는 {fmt_date(last['date'])}에 {fmt_price(last['price'])}에 거래됐고, {tail}")
                rows = "".join(f'<tr><td>{fmt_date(t["date"])}</td><td>{t["floor"]}층</td><td>{esc(fmt_price(t["price"]))}</td></tr>' for t in reversed(ts[-8:]))
                sections += (f'<div class="card"><h2>전용 {area}㎡ ({area / PYEONG:.0f}평형)</h2>{sparkline(ts)}'
                             f'<div class="sc"><table><thead><tr><th>계약일</th><th>층</th><th>거래가</th></tr></thead><tbody>{rows}</tbody></table></div></div>')
            built = f"{c['built']}년 준공 · " if c["built"] else ""
            body = (f'<p class="mu"><a href="r-{reg["code"]}.html">{esc(reg["name"])}</a> · {esc(c["dong"])} · {built}조회 기간 거래 {len(c["trades"])}건</p>'
                    f'<h1>{esc(c["apt"])} 실거래가</h1><div class="card"><p style="margin:0">{esc(" ".join(sentences))}</p></div>{site.ad()}{sections}')
            site.page(f"c-{reg['code']}-{c['id']}.html", f"{reg['name']} {c['dong']} {c['apt']} 실거래가",
                      f"{c['dong']} {c['apt']} 전용면적별 최근 거래가와 최고가 대비 현황. 최근 거래 {fmt_date(c['trades'][-1]['date'])} 기준.",
                      body, crumbs=[(cfg["site_name"], "index.html"), (reg["name"], f"r-{reg['code']}.html"),
                                    (c["apt"], f"c-{reg['code']}-{c['id']}.html")])

    # 메인
    all_highs = []
    for a in analyses:
        for h in a["highs"]:
            all_highs.append((a["region"], h))
    all_highs.sort(key=lambda x: (x[1]["price"] / x[1]["prev"]), reverse=True)
    hrows = "".join(
        f'<tr><td><a href="c-{r["code"]}-{h["cid"]}.html">{esc(h["apt"])}</a><div class="mu">{esc(r["name"])} {esc(h["dong"])}</div></td>'
        f'<td>{h["area"]}㎡</td><td>{esc(fmt_price(h["price"]))}</td><td class="up">+{(h["price"] / h["prev"] - 1) * 100:.1f}%</td></tr>'
        for r, h in all_highs[:12]) or '<tr><td colspan="4" class="mu">집계 중입니다.</td></tr>'
    cards = ""
    for a in analyses:
        r = a["region"]
        ms = sorted(a["monthly"].keys(), reverse=True)
        c0 = a["monthly"][ms[0]] if ms else {"count": 0, "median_py": 0}
        cards += (f'<div class="card"><h2><a href="r-{r["code"]}.html">{esc(r["name"])}</a></h2>'
                  f'<p class="mu" style="margin:0">이번 달 {c0["count"]}건 · 평당 중위 {c0["median_py"]:,.0f}만원<br>최고가 경신 {len(a["highs"])}곳</p></div>')
    body = (f'<h1>매일 갱신하는 아파트 실거래가 리포트</h1>'
            f'<p class="mu" style="margin:0">국토교통부 실거래가 공개 데이터를 매일 자동으로 정리해 지역별 거래 동향과 최고가 경신 단지를 보여줍니다.</p>'
            f'{site.ad()}<div class="card"><h2>최근 {cfg["new_high_window_days"]}일 최고가 경신 상위</h2><div class="sc"><table><thead><tr><th>단지</th><th>면적</th><th>거래가</th><th>직전 최고가 대비</th></tr></thead><tbody>{hrows}</tbody></table></div></div>'
            f'<div class="grid">{cards}</div>')
    site.page("index.html", f"{cfg['site_name']} 아파트 실거래가 최고가 경신 단지 매일 갱신",
              "국토교통부 실거래가 공개 데이터를 매일 자동 분석해 지역별 거래량, 평당가 변동, 최고가 경신 단지를 알려드립니다.", body,
              crumbs=[(cfg["site_name"], "index.html")])

    # 필수 안내 페이지
    about = (f'<h1>사이트 소개</h1><div class="card"><p>{esc(cfg["site_name"])}는 국토교통부가 공공데이터포털에 공개한 아파트 매매 실거래가를 '
             '매일 자동으로 수집해 지역별 거래량, 평당 중위가격, 최고가 경신 단지를 정리하는 사이트입니다.</p>'
             '<p>거래 정보는 계약 후 최대 30일 안에 신고되므로 최근 거래가 누락되어 있을 수 있습니다. 계약이 해제된 거래는 제외합니다. '
             '이 사이트의 정보는 투자나 거래 판단의 근거가 아니며, 정확한 내용은 국토교통부 실거래가 공개시스템에서 확인해야 합니다.</p>'
             f'<p>문의: {esc(cfg.get("contact_email", ""))}</p></div>')
    site.page("about.html", "사이트 소개", f"{cfg['site_name']}의 데이터 출처와 운영 방식을 소개합니다.", about)
    priv = ('<h1>개인정보처리방침</h1><div class="card"><p>이 사이트는 회원가입을 받지 않으며 이름, 연락처 같은 개인정보를 직접 수집하지 않습니다.</p>'
            '<p>방문 통계와 광고 제공을 위해 구글 애드센스, 구글 애널리틱스 등 제3자 서비스가 쿠키를 사용할 수 있습니다. 이용자는 브라우저 설정에서 쿠키를 거부할 수 있으며, '
            '구글의 광고 쿠키는 구글 광고 설정 페이지에서 관리할 수 있습니다.</p>'
            f'<p>문의: {esc(cfg.get("contact_email", ""))}</p></div>')
    site.page("privacy.html", "개인정보처리방침", f"{cfg['site_name']}의 개인정보 및 쿠키 처리 방침입니다.", priv)
    site.page("404.html", "페이지를 찾을 수 없습니다", "요청한 페이지가 없습니다.",
              '<h1>페이지를 찾을 수 없습니다</h1><p><a href="index.html">처음으로 돌아가기</a></p>', index=False)

    if not demo:
        base = cfg["site_url"].rstrip("/")
        urls = "".join(f"<url><loc>{esc(base + '/' + ('' if p == 'index.html' else p))}</loc><lastmod>{today.isoformat()}</lastmod></url>"
                       for p in site.pages if p not in ("404.html",))
        site.write("sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', index=False)
        site.write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n", index=False)
    return len(site.pages)


# ---------------------------------------------------------------- 진입점
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="샘플 데이터로 preview/ 생성 (공개 금지)")
    ap.add_argument("--check", action="store_true", help="API 연결과 응답 형식 확인")
    args = ap.parse_args()
    cfg = load_config()
    today = dt.date.today()

    if args.check:
        key = os.environ.get("DATA_GO_KR_KEY", "").strip()
        if not key:
            sys.exit("환경변수 DATA_GO_KR_KEY 를 먼저 설정하세요.")
        reg, ym = cfg["regions"][0], month_list(2, today)[1]
        raw = api_request(key, reg["code"], ym, 1, rows=3)
        print("원본 응답 앞부분:\n", raw[:700].decode("utf-8", "replace"), "\n")
        items, total = parse_response(raw)
        print(f"전체 {total}건, 이번 확인에서 {len(items)}건 수신")
        if items:
            print("항목 태그:", ", ".join(items[0].keys()))
            recs = to_records(items)
            print("변환 성공" if recs else "변환 실패: 태그 이름이 예상과 다릅니다. 위 태그 목록을 확인하세요.")
        return

    if args.demo:
        data, outdir = collect_demo(cfg, today), os.path.join(HERE, "preview")
    else:
        data, outdir = collect_live(cfg, today), os.path.join(HERE, "public")
    n = build_site(cfg, data, outdir, args.demo)
    print(f"완료: {n}개 페이지 → {outdir}")


if __name__ == "__main__":
    main()
