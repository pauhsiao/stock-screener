"""
用 Playwright 爬 TAIFEX 三大法人期貨未平倉（外資台指期淨口數）
"""
import re
import sys

def get_taifex_futures_position() -> dict:
    """
    回傳 dict:
      foreign_net: 外資台指期淨口數（正=多單，負=空單）
      it_net: 投信淨口數
      dealer_net: 自營商淨口數
      total_net: 三大法人合計
      date: 資料日期
    失敗時回傳 None
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[warn] playwright 未安裝", file=sys.stderr)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(
                "https://www.taifex.com.tw/cht/3/futContractsDate",
                wait_until="networkidle", timeout=30000,
            )

            # 等待表格載入
            page.wait_for_selector("table.table_f", timeout=15000)

            # 取得頁面 HTML 解析
            content = page.content()
            browser.close()

        return _parse_taifex_html(content)
    except Exception as e:
        print(f"[warn] TAIFEX Playwright 失敗: {e}", file=sys.stderr)
        return None


def _parse_taifex_html(html: str) -> dict | None:
    """從 TAIFEX 期貨未平倉 HTML 解析三大法人淨口數"""
    try:
        # 找日期
        date_match = re.search(r"日期[：:]\s*(\d{4}/\d{2}/\d{2})", html)
        date_str = date_match.group(1).replace("/", "-") if date_match else ""

        # 找台指期（TX）的三大法人淨口數
        # 表格結構：身份別 | 多方口數 | 空方口數 | 淨口數
        # 外資及陸資 / 投信 / 自營商

        def extract_net(label: str) -> int:
            pattern = rf"{label}.*?(-?[\d,]+)</td>"
            matches = re.findall(pattern, html, re.DOTALL)
            # 第3個 match 通常是淨口數
            if len(matches) >= 3:
                return int(matches[2].replace(",", ""))
            return 0

        # 台指期區塊
        tx_block = re.search(r"臺股期貨.*?(?=臺股期貨|$)", html, re.DOTALL)
        if not tx_block:
            return None
        block = tx_block.group(0)

        # 逐行解析數字
        numbers = [int(n.replace(",", "")) for n in re.findall(r"-?[\d,]{1,10}", block)
                   if len(n.replace(",", "").replace("-", "")) >= 3]

        # 簡化處理：直接找表格的 td 數值
        tds = re.findall(r"<td[^>]*>\s*(-?[\d,]+)\s*</td>", block)
        nums = [int(t.replace(",", "")) for t in tds if t.replace(",", "").lstrip("-").isdigit()]

        if len(nums) < 9:
            return None

        # 格式：外資(多,空,淨) / 投信(多,空,淨) / 自營(多,空,淨)
        foreign_net = nums[2]
        it_net = nums[5]
        dealer_net = nums[8]
        total_net = foreign_net + it_net + dealer_net

        return {
            "foreign_net": foreign_net,
            "it_net": it_net,
            "dealer_net": dealer_net,
            "total_net": total_net,
            "date": date_str,
        }
    except Exception as e:
        print(f"[warn] TAIFEX 解析失敗: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    result = get_taifex_futures_position()
    print(result)
