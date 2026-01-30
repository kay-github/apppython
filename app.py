import time
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
    fig.add_hline(
        y=mean_discount,
        line_dash="dash",
        line_color="#f04d3a",
        line_width=1.4,
        annotation_text=f"均值 {mean_discount:.2f}折",
        annotation_position="right",
    )

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

    buttons = []
    for label, start_date in periods.items():
        period_df = df[df.index >= start_date]
        if len(period_df) > 0:
            stats = (
                f"<b>统计（{label}）</b><br>"
                f"最新 {period_df['discount'].iloc[-1]:.2f}折<br>"
                f"最高 {period_df['discount'].max():.2f}折<br>"
                f"最低 {period_df['discount'].min():.2f}折<br>"
                f"均值 {period_df['discount'].mean():.2f}折"
            )
        else:
            stats = "暂无数据"

        buttons.append(
            dict(
                label=label,
                method="relayout",
                args=[{
                    "xaxis.range": [start_date, end_date],
                    "annotations[0].text": stats,
                }],
            )
        )

    fig.update_layout(
        xaxis_title="日期",
        yaxis_title="折扣（折）",
        hovermode="x unified",
        showlegend=True,
        legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99),
        updatemenus=[
            dict(
                type="buttons",
                direction="right",
                active=3,
                x=0.5,
                y=1.06,
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
        annotations=[
            dict(
                x=0.02,
                y=0.98,
                xref="paper",
                yref="paper",
                text=(
                    f"<b>统计（近半年）</b><br>"
                    f"最新 {df['discount'].iloc[-1]:.2f}折<br>"
                    f"最高 {df['discount'].max():.2f}折<br>"
                    f"最低 {df['discount'].min():.2f}折<br>"
                    f"均值 {df['discount'].mean():.2f}折"
                ),
                showarrow=False,
                font=dict(size=12),
                align="left",
                bgcolor="rgba(255, 255, 224, 0.9)",
                bordercolor="#c8d6f0",
                borderwidth=1,
                borderpad=8,
            )
        ],
        template="plotly_white",
        width=1200,
        height=680,
        margin=dict(t=140, b=50),
    )

    y_min = df["discount"].min() * 0.9
    y_max = df["discount"].max() * 1.1
    fig.update_yaxes(range=[y_min, y_max])

    return fig


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

    fig = _build_chart(df)
    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

    realtime_note = realtime_status
    generated_at = end_date.strftime("%Y-%m-%d %H:%M:%S")
    data_source = f"{source_zjxc} / {source_xys}"
    realtime_source = f"{rt_src_zjxc}/{rt_src_xys}"

    return f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>新易盛 / 中际旭创 市值比例图</title>
  <style>
    body {{ font-family: \"Segoe UI\", \"PingFang SC\", \"Microsoft YaHei\", sans-serif; margin: 16px 20px; }}
    .header {{ display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
    .title {{ font-size: 22px; font-weight: 700; color: #1f2a44; }}
    .meta-row {{ margin-top: 6px; color: #6b7280; font-size: 12.5px; display: flex; gap: 12px; flex-wrap: wrap; }}
    .chart-wrap {{ position: relative; width: 1200px; max-width: 100%; }}
    .btn {{ position: absolute; top: 52px; right: 6px; z-index: 5; padding: 6px 10px; border: 1px solid #c8d6f0; background: #eef3ff; color: #2b3b55; cursor: pointer; border-radius: 6px; font-size: 12px; }}
    .btn:hover {{ background: #e3ecff; }}
  </style>
</head>
<body>
  <div class=\"header\">
    <div class=\"title\">新易盛 / 中际旭创 市值比例图</div>
  </div>
  <div class=\"meta-row\">
    <div>数据日期：{data_date}</div>
    <div>使用实时行情：{realtime_note}</div>
    <div>生成时间：{generated_at}</div>
    <div>日线来源：{data_source}</div>
    <div>实时来源：{realtime_source}</div>
  </div>
  <div class=\"chart-wrap\">
    <button class=\"btn\" onclick=\"location.href='/?refresh=1&t=' + Date.now()\">刷新获取最新数据</button>
    {chart_html}
  </div>
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






