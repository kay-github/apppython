import time
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import plotly.graph_objects as go
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

app = FastAPI(title="市值比例图")

# -------------------------------
# Config
# -------------------------------
ZJXC_CODE = "sz.300308"
XYS_CODE = "sz.300502"

# Market cap anchors (CNY 100 million) used to infer total shares.
# Keep these aligned with the latest data date to reduce bias.
ZJXC_MARKET_CAP = 6840
XYS_MARKET_CAP = 3968

# Optional explicit total shares (100 million shares). If set, they override anchors.
ZJXC_SHARES = None
XYS_SHARES = None

USE_REALTIME = True
FETCH_RETRY = 1
RETRY_DELAY_SECONDS = 0
SINA_DATALEN = 300

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
CACHE_TTL_SECONDS = 20
_PAGE_CACHE = {"ts": 0.0, "html": None}


# -------------------------------
# Helpers
# -------------------------------

def _cn_now():
    return datetime.now(timezone(timedelta(hours=8)))


def _parse_code(code: str):
    code = code.strip()
    if code.lower().startswith("sz."):
        return code.split(".", 1)[1], "SZ"
    if code.lower().startswith("sh."):
        return code.split(".", 1)[1], "SH"
    if "." in code:
        num, market = code.split(".", 1)
        return num, market.upper()
    if len(code) == 6:
        market = "SZ" if code.startswith(("0", "3")) else "SH"
        return code, market
    return code, "SZ"


def _to_eastmoney_secid(code: str) -> str:
    num, market = _parse_code(code)
    market_id = "0" if market == "SZ" else "1"
    return f"{market_id}.{num}"


def _to_sina_symbol(code: str) -> str:
    num, market = _parse_code(code)
    prefix = "sh" if market == "SH" else "sz"
    return f"{prefix}{num}"


def _fetch_daily_close_from_em(code: str, start_date: str, end_date: str):
    secid = _to_eastmoney_secid(code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "klt": "101",
        "fqt": "0",
        "lmt": "300",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=6)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        klines = data.get("klines") or []
        if not klines:
            return None
        rows = []
        for k in klines:
            parts = k.split(",")
            if len(parts) < 3:
                continue
            rows.append([parts[0], parts[2]])
        df = pd.DataFrame(rows, columns=["date", "close"])
        df["date"] = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"])
        start = pd.to_datetime(start_date, format="%Y%m%d")
        end = pd.to_datetime(end_date, format="%Y%m%d")
        df = df[(df["date"] >= start) & (df["date"] <= end)].sort_values("date")
        return df[["date", "close"]]
    except Exception:
        return None


def _fetch_daily_close_from_sina(code: str, start_date: str, end_date: str):
    symbol = _to_sina_symbol(code)
    url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
    params = {
        "symbol": symbol,
        "scale": "240",
        "ma": "no",
        "datalen": str(SINA_DATALEN),
    }
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        rows = []
        for item in data:
            day = item.get("day")
            close = item.get("close")
            if not day or close is None:
                continue
            rows.append([day, close])
        df = pd.DataFrame(rows, columns=["date", "close"])
        df["date"] = pd.to_datetime(df["date"].str.slice(0, 10))
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["close"])
        start = pd.to_datetime(start_date, format="%Y%m%d")
        end = pd.to_datetime(end_date, format="%Y%m%d")
        df = df[(df["date"] >= start) & (df["date"] <= end)].sort_values("date")
        return df[["date", "close"]]
    except Exception:
        return None


def _fetch_daily_close(code: str, start_date: str, end_date: str):
    for _ in range(FETCH_RETRY):
        df = _fetch_daily_close_from_em(code, start_date, end_date)
        if df is not None and not df.empty:
            return df, "东方财富日线"
        if RETRY_DELAY_SECONDS:
            time.sleep(RETRY_DELAY_SECONDS)

    df = _fetch_daily_close_from_sina(code, start_date, end_date)
    if df is not None and not df.empty:
        return df, "新浪日线"
    return None, "无"


def _get_realtime_price_eastmoney(code: str):
    secid = _to_eastmoney_secid(code)
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": secid, "fields": "f43,f57,f58"}
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=4)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        price = data.get("f43")
        if price is None:
            return None
        price = float(price)
        if price > 1000:
            price = price / 100.0
        return price if price > 0 else None
    except Exception:
        return None


def _get_realtime_price_sina(code: str):
    symbol = _to_sina_symbol(code)
    url = f"http://hq.sinajs.cn/list={symbol}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=4)
        resp.raise_for_status()
        text = resp.text
        if "\"" not in text:
            return None
        raw = text.split('"', 1)[1].rsplit('"', 1)[0]
        parts = raw.split(",")
        if len(parts) < 4:
            return None
        price = float(parts[3])
        return price if price > 0 else None
    except Exception:
        return None


def _get_realtime_price_tencent(code: str):
    symbol = _to_sina_symbol(code)
    url = f"https://qt.gtimg.cn/q={symbol}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=4)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
        if "\"" not in text:
            return None
        raw = text.split("\"", 1)[1].rsplit("\"", 1)[0]
        parts = raw.split("~")
        if len(parts) < 4:
            return None
        price = float(parts[3])
        return price if price > 0 else None
    except Exception:
        return None


def _get_realtime_price(code: str):
    price = _get_realtime_price_eastmoney(code)
    if price is not None:
        return price, "东方财富"
    price = _get_realtime_price_sina(code)
    if price is not None:
        return price, "新浪"
    price = _get_realtime_price_tencent(code)
    if price is not None:
        return price, "腾讯"
    return None, "无"


def _build_chart(df: pd.DataFrame):
    hover_text = []
    for idx, row in df.iterrows():
        text = (
            f"<b>日期: {idx.strftime('%Y-%m-%d')}</b><br>"
            f"<b>中际旭创:</b> {row['zjxc_close']:.2f} / {row['zjxc_mv']:.0f}<br>"
            f"<b>新易盛:</b> {row['xys_close']:.2f} / {row['xys_mv']:.0f}<br>"
            f"<b>{row['discount']:.2f}折</b>"
        )
        hover_text.append(text)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df.index,
            y=df["discount"],
            mode="lines",
            name="市值比例",
            line=dict(color="#2f6fed", width=2.2),
            fill="tozeroy",
            fillcolor="rgba(47, 111, 237, 0.18)",
            hovertext=hover_text,
            hoverinfo="text",
        )
    )

    mean_discount = df["discount"].mean()
    fig.add_hline(y=mean_discount, line_dash="dash", line_color="#f04d3a", line_width=1.4)

    max_idx = df["discount"].idxmax()
    min_idx = df["discount"].idxmin()

    fig.add_trace(
        go.Scatter(
            x=[max_idx],
            y=[df["discount"].max()],
            mode="markers+text",
            name=f"最高 {df['discount'].max():.2f}折",
            marker=dict(color="#2aa84a", size=12, symbol="triangle-up"),
            text=[f"{df['discount'].max():.2f}折"],
            textposition="top center",
            hoverinfo="skip",
            showlegend=False,
        )
    )

    fig.add_trace(
        go.Scatter(
            x=[min_idx],
            y=[df["discount"].min()],
            mode="markers+text",
            name=f"最低 {df['discount'].min():.2f}折",
            marker=dict(color="#e13d3d", size=12, symbol="triangle-down"),
            text=[f"{df['discount'].min():.2f}折"],
            textposition="bottom center",
            hoverinfo="skip",
            showlegend=False,
        )
    )

    end_date = df.index[-1]
    periods = {
        "近1周": end_date - timedelta(days=7),
        "近1月": end_date - timedelta(days=30),
        "近1季": end_date - timedelta(days=90),
        "近半年": end_date - timedelta(days=180),
        "近1年": df.index[0],
    }

    period_details = {}
    period_cards = {}
    for label, start_date in periods.items():
        period_df = df[df.index >= start_date]
        if len(period_df) == 0:
            continue
        p_min = period_df["discount"].min()
        p_max = period_df["discount"].max()
        latest_val = period_df["discount"].iloc[-1]
        max_idx_period = period_df["discount"].idxmax()
        min_idx_period = period_df["discount"].idxmin()
        pad = max((p_max - p_min) * 0.2, 0.15)
        period_details[label] = {
            "start": start_date,
            "y_range": [max(0, p_min - pad), p_max + pad],
        }
        period_cards[label] = {
            "period": label,
            "latest": f"{latest_val:.2f}折",
            "latest_date": str(period_df.index[-1])[:10],
            "highest": f"{p_max:.2f}折",
            "highest_date": str(max_idx_period)[:10],
            "lowest": f"{p_min:.2f}折",
            "lowest_date": str(min_idx_period)[:10],
        }

    if not period_details:
        fallback_min = df["discount"].min()
        fallback_max = df["discount"].max()
        fallback_latest = df["discount"].iloc[-1]
        fallback_latest_date = str(df.index[-1])[:10]
        fallback_max_idx = df["discount"].idxmax()
        fallback_min_idx = df["discount"].idxmin()
        fallback_pad = max((fallback_max - fallback_min) * 0.2, 0.15)
        period_details["近1年"] = {
            "start": df.index[0],
            "y_range": [max(0, fallback_min - fallback_pad), fallback_max + fallback_pad],
        }
        period_cards["近1年"] = {
            "period": "近1年",
            "latest": f"{fallback_latest:.2f}折",
            "latest_date": fallback_latest_date,
            "highest": f"{fallback_max:.2f}折",
            "highest_date": str(fallback_max_idx)[:10],
            "lowest": f"{fallback_min:.2f}折",
            "lowest_date": str(fallback_min_idx)[:10],
        }

    default_label = "近半年" if "近半年" in period_details else next(iter(period_details.keys()))
    default_detail = period_details[default_label]

    buttons = []
    active_idx = 0
    for label in periods.keys():
        if label not in period_details:
            continue
        detail = period_details[label]
        if label == default_label:
            active_idx = len(buttons)
        buttons.append(
            dict(
                label=label,
                method="relayout",
                args=[{
                    "xaxis.range": [detail["start"], end_date],
                    "yaxis.range": detail["y_range"],
                }],
            )
        )

    fig.update_layout(
        xaxis=dict(title="日期", range=[default_detail["start"], end_date]),
        yaxis=dict(title="折扣（折）", range=default_detail["y_range"]),
        hovermode="x unified",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        updatemenus=[
            dict(
                type="buttons",
                direction="right",
                active=active_idx,
                x=0.5,
                y=1.14,
                xanchor="center",
                yanchor="bottom",
                buttons=buttons,
                showactive=True,
                bgcolor="#eef3ff",
                bordercolor="#c8d6f0",
                font=dict(size=12, color="#2b3b55"),
                pad=dict(r=6, t=6, l=6, b=6),
            )
        ],
        template="plotly_white",
        autosize=True,
        height=640,
        margin=dict(t=122, b=55, l=52, r=28),
    )

    return fig, period_cards, default_label


def _build_page():
    end_date = _cn_now()
    start_date = end_date - timedelta(days=365)
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    with ThreadPoolExecutor(max_workers=2) as executor:
        fut_zjxc = executor.submit(_fetch_daily_close, ZJXC_CODE, start_str, end_str)
        fut_xys = executor.submit(_fetch_daily_close, XYS_CODE, start_str, end_str)
        df_zjxc, source_zjxc = fut_zjxc.result()
        df_xys, source_xys = fut_xys.result()

    if df_zjxc is None or df_xys is None:
        return "<h2>获取日线数据失败（上游连接异常），请稍后重试。</h2>"

    df_zjxc = df_zjxc.set_index("date")
    df_xys = df_xys.set_index("date")

    df = pd.DataFrame()
    df["zjxc_close"] = df_zjxc["close"]
    df["xys_close"] = df_xys["close"]
    df = df.dropna()

    today = end_date.date()
    rt_zjxc = None
    rt_xys = None
    rt_src_zjxc = "无"
    rt_src_xys = "无"
    realtime_status = "否"
    if USE_REALTIME:
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_zjxc = executor.submit(_get_realtime_price, ZJXC_CODE)
            fut_xys = executor.submit(_get_realtime_price, XYS_CODE)
            rt_zjxc, rt_src_zjxc = fut_zjxc.result()
            rt_xys, rt_src_xys = fut_xys.result()
        if rt_zjxc is not None or rt_xys is not None:
            last_zjxc = df["zjxc_close"].iloc[-1]
            last_xys = df["xys_close"].iloc[-1]
            new_zjxc = rt_zjxc if rt_zjxc is not None else last_zjxc
            new_xys = rt_xys if rt_xys is not None else last_xys
            df.loc[pd.to_datetime(today)] = [new_zjxc, new_xys]
            df = df.sort_index()
            if rt_zjxc is not None and rt_xys is not None:
                realtime_status = "是"
            else:
                realtime_status = "部分"

    latest_zjxc_price = df["zjxc_close"].iloc[-1]
    latest_xys_price = df["xys_close"].iloc[-1]
    data_date = df.index[-1].strftime("%Y-%m-%d")

    if ZJXC_SHARES is not None and XYS_SHARES is not None:
        zjxc_shares = ZJXC_SHARES
        xys_shares = XYS_SHARES
    else:
        zjxc_shares = ZJXC_MARKET_CAP / latest_zjxc_price
        xys_shares = XYS_MARKET_CAP / latest_xys_price

    df["zjxc_mv"] = df["zjxc_close"] * zjxc_shares
    df["xys_mv"] = df["xys_close"] * xys_shares
    df["ratio"] = df["xys_mv"] / df["zjxc_mv"]
    df["discount"] = df["ratio"] * 10

    fig, period_cards, default_period = _build_chart(df)
    chart_config = {
        "responsive": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": [
            "zoom2d",
            "pan2d",
            "select2d",
            "lasso2d",
            "zoomIn2d",
            "zoomOut2d",
            "autoScale2d",
            "toImage",
            "toggleSpikelines",
        ],
    }
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", config=chart_config)
    default_card = period_cards.get(default_period, {})
    period_cards_json = json.dumps(period_cards, ensure_ascii=False)
    default_period_text = default_card.get("period", default_period)
    default_latest = default_card.get("latest", "--")
    default_latest_date = default_card.get("latest_date", "--")
    default_highest = default_card.get("highest", "--")
    default_highest_date = default_card.get("highest_date", "--")
    default_lowest = default_card.get("lowest", "--")
    default_lowest_date = default_card.get("lowest_date", "--")
    default_period_js = json.dumps(default_period, ensure_ascii=False)

    realtime_note = realtime_status
    generated_at = end_date.strftime("%Y-%m-%d %H:%M:%S")
    data_source = f"{source_zjxc} / {source_xys}"
    realtime_source = f"{rt_src_zjxc}/{rt_src_xys}"

    return f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\" />
  <title>新易盛 / 中际旭创 市值比例图</title>
  <link rel=\"icon\" href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='0.9em' font-size='90'%3E%F0%9F%93%88%3C/text%3E%3C/svg%3E\" />
  <style>
    :root {{
      --bg: linear-gradient(150deg, #f4f8ff 0%, #eef3ff 46%, #f7fbff 100%);
      --panel: #ffffff;
      --title: #172445;
      --muted: #5e6a7d;
      --line: #cfd8ea;
      --btn-bg: #eef3ff;
      --btn-bg-hover: #e3ecff;
      --btn-text: #2b3b55;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 14px;
      font-family: \"Noto Sans SC\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif;
      color: var(--title);
      background: var(--bg);
    }}
    .page {{
      width: min(1360px, 100%);
      margin: 0 auto;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 8px 28px rgba(17, 32, 67, 0.08);
    }}
    .header {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }}
    .title {{ font-size: 30px; font-weight: 800; letter-spacing: 0.2px; color: var(--title); }}
    .meta-row {{ margin-top: 8px; color: var(--muted); font-size: 12.8px; line-height: 1.5; display: flex; gap: 8px 14px; flex-wrap: wrap; }}
    .summary-panel {{
      margin-top: 12px;
      border: 1px solid #d2dcf3;
      border-radius: 12px;
      background: linear-gradient(180deg, #f9fbff 0%, #f2f6ff 100%);
      padding: 10px 12px;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9);
    }}
    .summary-top {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .summary-title {{ font-size: 13px; color: #3e4c67; font-weight: 700; }}
    .summary-period {{ font-size: 12px; color: #29406c; background: #e8efff; border: 1px solid #c7d7fa; border-radius: 999px; padding: 3px 10px; font-weight: 700; }}
    .summary-grid {{ margin-top: 8px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
    .summary-item {{ background: #ffffff; border: 1px solid #d8e1f5; border-radius: 10px; padding: 8px; }}
    .summary-k {{ font-size: 12px; color: #66758f; font-weight: 700; }}
    .summary-v {{ margin-top: 3px; font-size: 21px; line-height: 1.15; font-weight: 800; letter-spacing: 0.2px; }}
    .summary-v.latest {{ color: #2f6fed; }}
    .summary-v.highest {{ color: #1f9d52; }}
    .summary-v.lowest {{ color: #d74343; }}
    .summary-d {{ margin-top: 2px; font-size: 11.5px; color: #73809a; }}
    .chart-wrap {{ margin-top: 10px; width: 100%; min-height: 500px; }}
    .btn {{ padding: 6px 10px; border: 1px solid #c8d6f0; background: var(--btn-bg); color: var(--btn-text); cursor: pointer; border-radius: 8px; font-size: 11.8px; font-weight: 600; white-space: nowrap; }}
    .btn:hover {{ background: var(--btn-bg-hover); }}
    .chart-wrap > div {{ width: 100% !important; }}
    .js-plotly-plot, .plot-container {{ width: 100% !important; max-width: 100% !important; }}
    .modebar {{ transform: scale(0.9); transform-origin: top right; }}

    @media (max-width: 768px) {{
      body {{ padding: 10px; }}
      .page {{ border-radius: 12px; padding: 12px 10px; }}
      .title {{ font-size: 24px; line-height: 1.3; max-width: 100%; }}
      .meta-row {{ display: grid; grid-template-columns: 1fr; font-size: 12.8px; gap: 2px; }}
      .summary-panel {{ padding: 10px; }}
      .summary-title {{ font-size: 12.5px; }}
      .summary-period {{ font-size: 11.5px; padding: 2px 8px; }}
      .summary-grid {{ gap: 6px; }}
      .summary-item {{ padding: 7px; }}
      .summary-k {{ font-size: 11.5px; }}
      .summary-v {{ font-size: 18px; }}
      .summary-d {{ font-size: 10.5px; }}
      .header {{ align-items: flex-start; }}
      .btn {{ width: auto; text-align: center; font-size: 11.5px; padding: 6px 10px; border-radius: 7px; }}
      .chart-wrap {{ min-height: 430px; }}
      .modebar {{ transform: scale(0.84); transform-origin: top right; }}
    }}
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"header\">
      <div class=\"title\">新易盛 / 中际旭创 市值比例图</div>
      <button class=\"btn\" onclick=\"location.href='/?refresh=1&t=' + Date.now()\">刷新获取最新数据</button>
    </div>
    <div class=\"meta-row\">
      <div>数据日期：{data_date}</div>
      <div>使用实时行情：{realtime_note}</div>
      <div>生成时间：{generated_at}</div>
      <div>日线来源：{data_source}</div>
      <div>实时来源：{realtime_source}</div>
    </div>
    <div class=\"summary-panel\">
      <div class=\"summary-top\">
        <div class=\"summary-title\">周期统计（固定）</div>
        <div id=\"periodLabel\" class=\"summary-period\">{default_period_text}</div>
      </div>
      <div class=\"summary-grid\">
        <div class=\"summary-item\">
          <div class=\"summary-k\">最新</div>
          <div id=\"latestVal\" class=\"summary-v latest\">{default_latest}</div>
          <div id=\"latestDate\" class=\"summary-d\">{default_latest_date}</div>
        </div>
        <div class=\"summary-item\">
          <div class=\"summary-k\">最高</div>
          <div id=\"highestVal\" class=\"summary-v highest\">{default_highest}</div>
          <div id=\"highestDate\" class=\"summary-d\">{default_highest_date}</div>
        </div>
        <div class=\"summary-item\">
          <div class=\"summary-k\">最低</div>
          <div id=\"lowestVal\" class=\"summary-v lowest\">{default_lowest}</div>
          <div id=\"lowestDate\" class=\"summary-d\">{default_lowest_date}</div>
        </div>
      </div>
    </div>
    <div class=\"chart-wrap\">
      {chart_html}
    </div>
  </div>
  <script>
    (function () {{
      var periodStatsMap = {period_cards_json};
      var defaultPeriod = {default_period_js};

      function updateSummaryCard(label) {{
        var stat = periodStatsMap[label];
        if (!stat) return;
        document.getElementById('periodLabel').textContent = stat.period || label;
        document.getElementById('latestVal').textContent = stat.latest || '--';
        document.getElementById('latestDate').textContent = stat.latest_date || '--';
        document.getElementById('highestVal').textContent = stat.highest || '--';
        document.getElementById('highestDate').textContent = stat.highest_date || '--';
        document.getElementById('lowestVal').textContent = stat.lowest || '--';
        document.getElementById('lowestDate').textContent = stat.lowest_date || '--';
      }}

      function relayoutForScreen() {{
        var gd = document.querySelector('.js-plotly-plot');
        if (!gd || !window.Plotly) return;
        var wrap = document.querySelector('.chart-wrap');
        var targetWidth = wrap ? Math.max(320, wrap.clientWidth - 2) : Math.max(320, gd.clientWidth);
        var isMobile = window.matchMedia('(max-width: 768px)').matches;
        var mobileUpdates = {{
          'width': targetWidth,
          'height': 470,
          'margin.t': 118,
          'margin.r': 10,
          'margin.l': 42,
          'margin.b': 52,
          'legend.orientation': 'h',
          'legend.x': 0,
          'legend.y': 1.02,
          'updatemenus[0].x': 0.5,
          'updatemenus[0].y': 1.09,
          'updatemenus[0].xanchor': 'center',
          'updatemenus[0].direction': 'right',
          'updatemenus[0].font.size': 10
        }};
        var desktopUpdates = {{
          'width': targetWidth,
          'height': 640,
          'margin.t': 122,
          'margin.r': 28,
          'margin.l': 52,
          'margin.b': 55,
          'legend.orientation': 'h',
          'legend.x': 0,
          'legend.y': 1.02,
          'updatemenus[0].x': 0.5,
          'updatemenus[0].y': 1.1,
          'updatemenus[0].xanchor': 'center',
          'updatemenus[0].direction': 'right',
          'updatemenus[0].font.size': 12
        }};
        window.Plotly.relayout(gd, isMobile ? mobileUpdates : desktopUpdates);
        window.Plotly.Plots.resize(gd);
      }}

      function bindPeriodButton(gd) {{
        if (!gd || gd.__periodBound) return;
        gd.__periodBound = true;
        gd.on('plotly_buttonclicked', function (event) {{
          if (!event || !event.button || !event.button.label) return;
          updateSummaryCard(event.button.label);
        }});
      }}

      function init() {{
        var gd = document.querySelector('.js-plotly-plot');
        if (!gd || !window.Plotly) {{
          window.setTimeout(init, 80);
          return;
        }}
        bindPeriodButton(gd);
        updateSummaryCard(defaultPeriod);
        relayoutForScreen();
      }}

      window.addEventListener('load', init);
      window.addEventListener('resize', relayoutForScreen);
    }})();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    refresh = request.query_params.get("refresh") == "1"
    now_ts = time.time()
    if not refresh:
        cached_html = _PAGE_CACHE.get("html")
        cached_ts = _PAGE_CACHE.get("ts") or 0.0
        if cached_html and (now_ts - cached_ts) < CACHE_TTL_SECONDS:
            return cached_html
    html = _build_page()
    _PAGE_CACHE["ts"] = now_ts
    _PAGE_CACHE["html"] = html
    return html






