import os
from datetime import datetime, timedelta, date, timezone
import requests
import pandas as pd
import plotly.graph_objects as go
import akshare as ak
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Market Cap Ratio")

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
CACHE_TTL_SECONDS = 300

_CACHE = {"ts": None, "html": None}


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


def _to_ak_symbol(code: str) -> str:
    num, _ = _parse_code(code)
    return num


def _to_eastmoney_secid(code: str) -> str:
    num, market = _parse_code(code)
    market_id = "0" if market == "SZ" else "1"
    return f"{market_id}.{num}"


def _fetch_daily_close(symbol: str, start_date: str, end_date: str):
    df = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust="",
    )
    if df is None or df.empty:
        return None
    df = df[["日期", "收盘"]].copy()
    df["date"] = pd.to_datetime(df["日期"])
    df["close"] = pd.to_numeric(df["收盘"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("date")
    return df[["date", "close"]]


def _is_trade_day(target: date) -> bool:
    try:
        cal = ak.tool_trade_date_hist_sina()
        if cal is None or cal.empty:
            return target.weekday() < 5
        cal["trade_date"] = pd.to_datetime(cal["trade_date"]).dt.date
        max_date = max(cal["trade_date"])
        if target > max_date:
            return target.weekday() < 5
        return target in set(cal["trade_date"])
    except Exception:
        return target.weekday() < 5


def _get_realtime_price_eastmoney(code: str):
    secid = _to_eastmoney_secid(code)
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        f"secid={secid}&fields=f43,f57,f58"
    )
    try:
        resp = requests.get(url, timeout=10)
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


def _build_chart(df: pd.DataFrame):
    hover_text = []
    for idx, row in df.iterrows():
        text = (
            f"<b>Date: {idx.strftime('%Y-%m-%d')}</b><br>"
            f"<b>ZJXC:</b> {row['zjxc_close']:.2f} / {row['zjxc_mv']:.0f}<br>"
            f"<b>XYS:</b> {row['xys_close']:.2f} / {row['xys_mv']:.0f}<br>"
            f"<b>{row['discount']:.2f}x</b>"
        )
        hover_text.append(text)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df.index,
            y=df["discount"],
            mode="lines",
            name="Market Cap Ratio",
            line=dict(color="#1f77b4", width=2),
            fill="tozeroy",
            fillcolor="rgba(31, 119, 180, 0.2)",
            hovertext=hover_text,
            hoverinfo="text",
        )
    )

    mean_discount = df["discount"].mean()
    fig.add_hline(
        y=mean_discount,
        line_dash="dash",
        line_color="red",
        line_width=1.5,
        annotation_text=f"Mean {mean_discount:.2f}x",
        annotation_position="right",
    )

    max_idx = df["discount"].idxmax()
    min_idx = df["discount"].idxmin()

    fig.add_trace(
        go.Scatter(
            x=[max_idx],
            y=[df["discount"].max()],
            mode="markers+text",
            name=f"Max {df['discount'].max():.2f}x",
            marker=dict(color="green", size=12, symbol="triangle-up"),
            text=[f"{df['discount'].max():.2f}x"],
            textposition="top center",
            hoverinfo="skip",
        )
    )

    fig.add_trace(
        go.Scatter(
            x=[min_idx],
            y=[df["discount"].min()],
            mode="markers+text",
            name=f"Min {df['discount'].min():.2f}x",
            marker=dict(color="red", size=12, symbol="triangle-down"),
            text=[f"{df['discount'].min():.2f}x"],
            textposition="bottom center",
            hoverinfo="skip",
        )
    )

    end_date = df.index[-1]
    periods = {
        "1W": end_date - timedelta(days=7),
        "1M": end_date - timedelta(days=30),
        "1Q": end_date - timedelta(days=90),
        "6M": end_date - timedelta(days=180),
        "1Y": df.index[0],
    }

    buttons = []
    for label, start_date in periods.items():
        period_df = df[df.index >= start_date]
        if len(period_df) > 0:
            stats = (
                f"<b>Stats ({label})</b><br>"
                f"Latest {period_df['discount'].iloc[-1]:.2f}x<br>"
                f"Max {period_df['discount'].max():.2f}x<br>"
                f"Min {period_df['discount'].min():.2f}x<br>"
                f"Mean {period_df['discount'].mean():.2f}x"
            )
        else:
            stats = "No data"

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
        title=dict(
            text="ZJXC vs XYS Market Cap Ratio",
            font=dict(size=20),
            x=0.5,
            y=0.97,
            yanchor="top",
        ),
        xaxis_title="Date",
        yaxis_title="Discount (x)",
        hovermode="x unified",
        showlegend=True,
        legend=dict(yanchor="top", y=0.99, xanchor="right", x=0.99),
        updatemenus=[
            dict(
                type="buttons",
                direction="right",
                active=3,
                x=0.5,
                y=1.04,
                xanchor="center",
                yanchor="bottom",
                buttons=buttons,
                showactive=True,
                bgcolor="lightgray",
                bordercolor="gray",
                font=dict(size=12),
            )
        ],
        annotations=[
            dict(
                x=0.02,
                y=0.98,
                xref="paper",
                yref="paper",
                text=(
                    f"<b>Stats (6M)</b><br>"
                    f"Latest {df['discount'].iloc[-1]:.2f}x<br>"
                    f"Max {df['discount'].max():.2f}x<br>"
                    f"Min {df['discount'].min():.2f}x<br>"
                    f"Mean {df['discount'].mean():.2f}x"
                ),
                showarrow=False,
                font=dict(size=12),
                align="left",
                bgcolor="rgba(255, 255, 224, 0.9)",
                bordercolor="gray",
                borderwidth=1,
                borderpad=8,
            )
        ],
        template="plotly_white",
        width=1200,
        height=700,
        margin=dict(t=180, b=60),
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

    zjxc_symbol = _to_ak_symbol(ZJXC_CODE)
    xys_symbol = _to_ak_symbol(XYS_CODE)

    df_zjxc = _fetch_daily_close(zjxc_symbol, start_str, end_str)
    df_xys = _fetch_daily_close(xys_symbol, start_str, end_str)

    if df_zjxc is None or df_xys is None:
        return "<h2>Failed to load daily data.</h2>"

    df_zjxc = df_zjxc.set_index("date")
    df_xys = df_xys.set_index("date")

    df = pd.DataFrame()
    df["zjxc_close"] = df_zjxc["close"]
    df["xys_close"] = df_xys["close"]
    df = df.dropna()

    today = end_date.date()
    used_realtime = False
    if USE_REALTIME and _is_trade_day(today):
        if df.index[-1].date() < today:
            rt_zjxc = _get_realtime_price_eastmoney(ZJXC_CODE)
            rt_xys = _get_realtime_price_eastmoney(XYS_CODE)
            if rt_zjxc is not None and rt_xys is not None:
                df.loc[pd.to_datetime(today)] = [rt_zjxc, rt_xys]
                df = df.sort_index()
                used_realtime = True

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

    realtime_note = "Yes" if used_realtime else "No"

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Market Cap Ratio</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    .meta {{ color: #555; margin-bottom: 12px; }}
  </style>
</head>
<body>
  <h1>Market Cap Ratio</h1>
  <div class="meta">
    Data date: {data_date} | Realtime used: {realtime_note}
  </div>
  {chart_html}
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    now = _cn_now().timestamp()
    cached_ts = _CACHE.get("ts")
    if cached_ts and _CACHE.get("html") and (now - cached_ts) < CACHE_TTL_SECONDS:
        return _CACHE["html"]

    html = _build_page()
    _CACHE["ts"] = now
    _CACHE["html"] = html
    return html
