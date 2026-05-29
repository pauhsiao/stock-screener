"""
每日台股全市場策略篩選器
流程：TWSE 官方 API 取前 300 大成交量股 → 法人連買初篩 → 技術面 → 基本面 → Pushover
"""
import os, urllib.request, urllib.parse, urllib.error, json, time, sys, io, tempfile
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

FINMIND_TOKEN  = os.environ.get("FINMIND_TOKEN", "")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")
FM_BASE = "https://api.finmindtrade.com/api/v4/data"

# 已知熱門族群（AI/記憶體/光通訊/PCB/設備等）
HOT_STOCKS = {
    "2330","2408","2344","3260","8299","2451","3081","2455",
    "3163","4979","2383","6274","3037","1815","3680","3583",
    "3131","6640","6187","2327","3008","2379","2308","2382",
    "3034","2303","2317","2454","4966","3711","6531","2395",
    "2357","6257","3714",
}

# ── 工具函式 ────────────────────────────────────────────────────

def fm_get(dataset, start, stock_id):
    url = (f"{FM_BASE}?dataset={dataset}&data_id={stock_id}"
           f"&start_date={start}&token={FINMIND_TOKEN}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("data", [])
    except Exception as e:
        print(f"  [warn] {dataset} {stock_id}: {e}", file=sys.stderr)
        return []

# 一次從 TWSE 取得全市場股票名稱對照表（不需 token）
_STOCK_NAMES: dict = {}

def load_stock_names():
    global _STOCK_NAMES
    if _STOCK_NAMES:
        return
    try:
        url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        _STOCK_NAMES = {d["公司代號"]: d["公司簡稱"] for d in data if "公司代號" in d}
        print(f"  → 股票名稱載入：{len(_STOCK_NAMES)} 檔")
    except Exception as e:
        print(f"  [warn] 股票名稱載入失敗: {e}", file=sys.stderr)

def get_stock_name(stock_id):
    if not _STOCK_NAMES:
        load_stock_names()
    return _STOCK_NAMES.get(stock_id, stock_id)

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [warn] GET {url}: {e}", file=sys.stderr)
        return None

def ma(lst, n):
    return sum(lst[-n:]) / n if len(lst) >= n else None

def days_back(n):
    return (datetime.today() - timedelta(days=int(n * 1.6))).strftime("%Y-%m-%d")

# ── Step 1：從 TWSE 取指定日期成交量前 300 檔 ───────────────────

def fetch_twse_stocks(date_str):
    """回傳指定日期 TWSE 全市場 [(stock_id, volume), ...]，無資料回傳 []。"""
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999"
    data = http_get(url)
    candidates = []
    if data and data.get("tables"):
        for table in data["tables"]:
            fields = table.get("fields", [])
            if "證券代號" in fields and "成交股數" in fields:
                code_idx = fields.index("證券代號")
                vol_idx  = fields.index("成交股數")
                for row in table.get("data", []):
                    code = row[code_idx].strip()
                    if code.isdigit() and len(code) == 4:
                        try:
                            vol = int(row[vol_idx].replace(",", ""))
                            candidates.append((code, vol))
                        except:
                            pass
    return candidates

def get_candidate_stocks():
    """
    依日期規則決定是否推播，回傳 stock_ids 或 None（跳過）。
    - 週日：跳過
    - 週六：取週五資料；若週五為假日則跳過
    - 平日：若今日或昨日（平日）無 TWSE 資料（國定假日）則跳過
    """
    today = datetime.today()
    dow = today.weekday()  # 0=Mon … 6=Sun

    if dow == 6:
        print("今日為週日，不推播")
        return None

    if dow == 5:  # 週六 → 取週五資料
        target = today - timedelta(days=1)
        date_str = target.strftime("%Y%m%d")
        print(f"Step 1: 週六 → 取週五（{target.strftime('%Y-%m-%d')}）成交量前 300 檔...")
        candidates = fetch_twse_stocks(date_str)
        if not candidates:
            print("  週五無交易資料（國定假日），跳過推播")
            return None

    else:  # 平日
        # 若昨天是平日且無交易資料 → 國定假日隔天，跳過
        yesterday = today - timedelta(days=1)
        if yesterday.weekday() < 5:
            if not fetch_twse_stocks(yesterday.strftime("%Y%m%d")):
                print(f"  昨日（{yesterday.strftime('%Y-%m-%d')}）為國定假日，今日不推播")
                return None

        date_str = today.strftime("%Y%m%d")
        print(f"Step 1: 從 TWSE 取今日成交量前 300 檔...")
        candidates = fetch_twse_stocks(date_str)
        if not candidates:
            print("  今日無交易資料（國定假日），跳過推播")
            return None

    candidates.sort(key=lambda x: -x[1])
    stock_ids = [c[0] for c in candidates[:300]]
    print(f"  → 候選股：{len(stock_ids)} 檔")
    return stock_ids

# 備用廣泛清單（覆蓋各族群，非僅熱門股）
_FALLBACK_LIST = [
    # 半導體/IC設計
    "2330","2303","2454","3034","3711","2379","2337","2385","2388","6533",
    "4966","2449","3029","6223","3167","2441","2436","2405","2408","3019",
    # 記憶體/儲存
    "2344","2408","3260","8299","2451","4967","3006","2421",
    # 光通訊/CPO
    "3081","2455","3163","4979","3491","6533","4961","3707",
    # PCB/CCL
    "2383","6274","3037","1815","2368","2358","8358","6269","3024",
    # 設備/耗材
    "3680","3583","3131","6640","6187","3558","5274","6550","3532",
    # 電子/伺服器
    "2382","2317","2357","2308","3231","2356","6415","3045","3023",
    # 被動元件
    "2327","2492","2489","2312",
    # 光電/LED
    "3714","2393","3707","2393",
    # 金融/傳產（避免只看科技）
    "2881","2882","2884","2886","2891","2892","1301","1303","2002",
    # 其他電子
    "2395","3044","4916","6669","3711","2239","6285","3653","6409",
]

# ── Step 2：法人連買初篩（逐股，但只篩前300） ────────────────────

def get_chip_streaks(stock_ids):
    print(f"\nStep 2: 法人籌碼初篩（{len(stock_ids)} 檔）...")
    start = days_back(15)
    result = {}
    for sid in stock_ids:
        time.sleep(0.25)
        inst = fm_get("TaiwanStockInstitutionalInvestorsBuySell", start, sid)
        if not inst:
            continue
        dates = sorted(set(d["date"] for d in inst))[-8:]
        fi_streak = 0
        for dt in reversed(dates):
            nets = {d["name"]: d["buy"] - d["sell"] for d in inst if d["date"] == dt}
            if nets.get("Foreign_Investor", 0) > 0:
                fi_streak += 1
            else:
                break
        it_streak = 0
        for dt in reversed(dates):
            nets = {d["name"]: d["buy"] - d["sell"] for d in inst if d["date"] == dt}
            if nets.get("Investment_Trust", 0) > 0:
                it_streak += 1
            else:
                break
        if fi_streak >= 3 or it_streak >= 3:
            result[sid] = (fi_streak, it_streak)

    print(f"  → 法人連買 ≥3天：{len(result)} 檔")
    return result

# ── Step 3：技術面篩選 ──────────────────────────────────────────

def check_technical(stock_id):
    prices = fm_get("TaiwanStockPrice", days_back(90), stock_id)
    if len(prices) < 65:
        return None, 0
    closes = [p["close"] for p in prices]
    vols   = [p["Trading_Volume"] for p in prices]
    ma5, ma10, ma20, ma60 = ma(closes, 5), ma(closes, 10), ma(closes, 20), ma(closes, 60)
    if not all([ma5, ma10, ma20, ma60]):
        return None, 0
    last    = closes[-1]
    vol5avg = ma(vols, 5)
    above_ma   = last > ma20 and last > ma60
    golden     = ma5 > ma10 > ma20 > ma60
    near_high  = last >= max(closes[-20:]) * 0.97
    vol_expand = vols[-1] > vol5avg * 1.3
    score = sum([above_ma, golden, near_high, vol_expand])
    return {
        "price": last,
        "ma5": round(ma5, 1), "ma20": round(ma20, 1), "ma60": round(ma60, 1),
        "above_ma": above_ma, "golden": golden,
        "near_high": near_high, "vol_expand": vol_expand,
    }, score

# ── Step 4：基本面加分 ──────────────────────────────────────────

def check_revenue_yoy(stock_id):
    rev_data = fm_get("TaiwanStockMonthRevenue", days_back(400), stock_id)
    rev_map = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in rev_data}
    today = datetime.today()
    yoy_list, consec = [], 0
    for i in range(6):
        d = today.replace(day=1) - timedelta(days=i * 28)
        y, m = d.year, d.month
        curr = rev_map.get((y, m))
        prev = rev_map.get((y - 1, m))
        if curr and prev and prev > 0:
            yoy_list.append(round((curr - prev) / prev * 100, 1))
    for v in yoy_list:
        if v >= 20:
            consec += 1
        else:
            break
    return yoy_list[:4], consec

def check_eps(stock_id):
    eps_data = fm_get("TaiwanStockFinancialStatements", days_back(500), stock_id)
    vals = sorted(
        [(e["date"], float(e["value"])) for e in eps_data
         if e.get("type") == "EPS" and e.get("value") is not None],
        key=lambda x: x[0]
    )[-5:]
    if len(vals) < 2:
        return vals, False
    values = [v for _, v in vals]
    return vals, values[-1] >= max(values[:-1])

# ── 圖表生成 ─────────────────────────────────────────────────────

def generate_chart(stock_id: str, stock_name: str) -> str | None:
    """生成個股 K 線圖，回傳暫存檔路徑，失敗回傳 None"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        prices = fm_get("TaiwanStockPrice", days_back(40), stock_id)
        if len(prices) < 10:
            return None

        dates  = [p["date"] for p in prices[-30:]]
        opens  = [float(p["open"])  for p in prices[-30:]]
        highs  = [float(p["max"])   for p in prices[-30:]]
        lows   = [float(p["min"])   for p in prices[-30:]]
        closes = [float(p["close"]) for p in prices[-30:]]
        vols   = [float(p["Trading_Volume"]) for p in prices[-30:]]

        xs = list(range(len(dates)))
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 5),
                                        gridspec_kw={"height_ratios": [3, 1]},
                                        facecolor="#1a1a2e")
        for ax in (ax1, ax2):
            ax.set_facecolor("#1a1a2e")
            ax.tick_params(colors="#aaaaaa", labelsize=7)
            for spine in ax.spines.values():
                spine.set_edgecolor("#333355")

        # K 線
        for i, x in enumerate(xs):
            color = "#ff4b4b" if closes[i] >= opens[i] else "#00cc44"
            ax1.plot([x, x], [lows[i], highs[i]], color=color, linewidth=1)
            ax1.bar(x, abs(closes[i] - opens[i]),
                    bottom=min(opens[i], closes[i]),
                    color=color, width=0.6, alpha=0.9)

        # 均線
        def sma(lst, n):
            return [sum(lst[max(0, i-n+1):i+1]) / min(i+1, n) for i in range(len(lst))]

        for n, color in ((5, "#ffd700"), (20, "#00bfff")):
            ma_vals = sma(closes, n)
            ax1.plot(xs, ma_vals, color=color, linewidth=1, label=f"MA{n}", alpha=0.8)

        ax1.legend(loc="upper left", fontsize=7, facecolor="#1a1a2e",
                   labelcolor="white", framealpha=0.5)
        ax1.set_title(f"{stock_id} {stock_name}", color="white", fontsize=10, pad=6)
        ax1.set_xlim(-0.5, len(xs) - 0.5)
        ax1.set_xticks([])

        # 成交量
        vol_colors = ["#ff4b4b" if closes[i] >= opens[i] else "#00cc44" for i in xs]
        ax2.bar(xs, vols, color=vol_colors, width=0.6, alpha=0.8)
        ax2.set_xlim(-0.5, len(xs) - 0.5)
        ax2.set_xticks(xs[::5])
        ax2.set_xticklabels([dates[i][5:] for i in xs[::5]], rotation=0, fontsize=6, color="#aaaaaa")

        plt.tight_layout(pad=0.5)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        plt.savefig(tmp.name, dpi=120, bbox_inches="tight", facecolor="#1a1a2e")
        plt.close(fig)
        return tmp.name
    except Exception as e:
        print(f"[warn] 圖表生成失敗 {stock_id}: {e}", file=sys.stderr)
        return None


# ── Pushover ─────────────────────────────────────────────────────

def send_pushover(title, message, image_path=None):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        print("Pushover 未設定，跳過推播")
        return
    if image_path:
        _send_pushover_multipart(title, message, image_path)
    else:
        payload = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                   "title": title, "message": message, "html": 1}
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json", data=data, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"Pushover: {'成功' if r.status == 200 else r.status}")
        except Exception as e:
            print(f"Pushover 失敗: {e}")


def _send_pushover_multipart(title, message, image_path):
    """帶圖片附件的 multipart/form-data 推播"""
    import mimetypes
    boundary = "----PushoverBoundary"
    with open(image_path, "rb") as f:
        img_data = f.read()

    def field(name, value):
        return (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n").encode()

    body = (
        field("token",   PUSHOVER_TOKEN) +
        field("user",    PUSHOVER_USER) +
        field("title",   title) +
        field("message", message) +
        field("html",    "1") +
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="attachment"; filename="chart.png"\r\n'
         f"Content-Type: image/png\r\n\r\n").encode() +
        img_data + b"\r\n" +
        f"--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"Pushover(圖片): {'成功' if r.status == 200 else r.status}")
    except Exception as e:
        print(f"Pushover(圖片)失敗: {e}")
        # fallback：不帶圖片再傳一次
        send_pushover(title, message)

# ── 主流程 ──────────────────────────────────────────────────────

def run():
    today_str = datetime.today().strftime("%Y-%m-%d")
    print(f"\n=== 台股全市場策略篩選 {today_str} ===\n")

    # ── 預載股票名稱（TWSE 一次取全部，不需 token）──────────────
    print("預載股票名稱...")
    load_stock_names()

    # ── 0. 期貨訊號（Playwright）────────────────────────────────
    print("Step 0: 爬 TAIFEX 期貨未平倉...")
    try:
        from playwright_data import get_taifex_futures_position
        futures = get_taifex_futures_position()
    except Exception:
        futures = None
    if futures:
        print(f"  外資台指期淨口數: {futures['foreign_net']:+,} 口")
    else:
        print("  期貨資料取得失敗，略過")

    # ── 1–4. 原有篩選流程 ────────────────────────────────────────
    stock_ids = get_candidate_stocks()
    if stock_ids is None:
        return

    chip_map = get_chip_streaks(stock_ids)

    print(f"\nStep 3: 技術面篩選（{len(chip_map)} 檔）...")
    tech_passed = []
    for sid, (fi, it) in chip_map.items():
        time.sleep(0.3)
        tech, score = check_technical(sid)
        if tech and score >= 2:
            tech_passed.append({"id": sid, "fi": fi, "it": it, "tech_score": score, **tech})
    print(f"  → 技術面通過：{len(tech_passed)} 檔")

    print(f"\nStep 4: 基本面查詢（{len(tech_passed)} 檔）...")
    results = []
    for r in tech_passed:
        time.sleep(0.3)
        yoy_list, yoy_consec = check_revenue_yoy(r["id"])
        time.sleep(0.3)
        _, eps_high = check_eps(r["id"])
        chip_score = 2 if (r["fi"] >= 3 or r["it"] >= 3) else 0
        fund_score = (2 if yoy_consec >= 3 else 1 if yoy_consec >= 1 else 0) + (1 if eps_high else 0)
        total = r["tech_score"] + chip_score + fund_score
        results.append({**r, "yoy_consec": yoy_consec, "yoy_list": yoy_list,
                         "eps_high": eps_high, "total": total})
        print(f"  {r['id']}: tech={r['tech_score']} fi={r['fi']}d it={r['it']}d yoy={yoy_consec}月 eps={eps_high} total={total}")

    results.sort(key=lambda x: -x["total"])
    hot    = [r for r in results if r["id"] in HOT_STOCKS][:4]
    horses = [r for r in results if r["id"] not in HOT_STOCKS][:3]

    for r in hot + horses:
        r["name"] = get_stock_name(r["id"])

    # ── 推播 1：市場方向訊號 ─────────────────────────────────────
    if futures:
        fn = futures["foreign_net"]
        direction = "📈 偏多" if fn > 5000 else ("📉 偏空" if fn < -5000 else "⚖️ 中性")
        sig_lines = [
            f"<b>🌐 {today_str} 大盤方向訊號</b>",
            f"外資台指期淨口數：<b>{fn:+,} 口</b>  {direction}",
            f"投信：{futures['it_net']:+,} 口　自營商：{futures['dealer_net']:+,} 口",
            f"三大法人合計：<b>{futures['total_net']:+,} 口</b>",
        ]
        send_pushover(f"🌐 大盤方向 {today_str}", "\n".join(sig_lines))
        time.sleep(1)

    # ── 推播 2：個股精選（最高分附 K 線圖）──────────────────────
    def fmt(i, r):
        tags = []
        if r["golden"]:     tags.append("多頭排列✅")
        if r["near_high"]:  tags.append("近高點✅")
        if r["vol_expand"]: tags.append("量放✅")
        chip = f"外資{r['fi']}天" if r["fi"] >= r["it"] else f"投信{r['it']}天"
        yoy_str = f"營收連{r['yoy_consec']}月+20%" if r["yoy_consec"] >= 1 else ""
        eps_str = "EPS創高" if r["eps_high"] else ""
        extras = " | ".join(filter(None, [yoy_str, eps_str]))
        return (
            f"<b>{i}. {r['id']} {r['name']}</b> ${r['price']:.0f}\n"
            f"   {' '.join(tags) or '技術偏弱'} {chip}"
            + (f"\n   {extras}" if extras else "")
        )

    lines = [f"<b>📈 {today_str} 台股策略篩選</b>"]
    lines.append(f"<i>全市場 {len(stock_ids)} 檔 → 法人初篩 {len(chip_map)} 檔 → 技術通過 {len(tech_passed)} 檔</i>\n")
    lines.append("<b>🔥 熱門族群精選</b>")
    lines += [fmt(i, r) for i, r in enumerate(hot, 1)] or ["今日無符合標的"]
    lines.append("\n<b>🐎 黑馬（市場較少討論）</b>")
    lines += [fmt(i, r) for i, r in enumerate(horses, 1)] or ["今日無黑馬標的"]

    msg = "\n".join(lines)
    print("\n--- Pushover 訊息 ---")
    print(msg)

    # 最高分股票附 K 線圖
    top = (hot + horses)
    chart_path = None
    if top:
        print(f"\n生成 {top[0]['id']} K 線圖...")
        chart_path = generate_chart(top[0]["id"], top[0]["name"])

    send_pushover(f"📊 台股策略推薦 {today_str}", msg, image_path=chart_path)

    if chart_path:
        try:
            os.remove(chart_path)
        except Exception:
            pass


if __name__ == "__main__":
    run()
