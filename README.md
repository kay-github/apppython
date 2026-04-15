---
title: kaypycode
emoji: 📈
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# 新易盛 / 中际旭创 市值比例图

这是一个基于 FastAPI 的 Hugging Face Docker Space，提供单页可视化，展示两只 A 股在最近一年内的市值比例与折扣（`ratio * 10`）。最新点使用实时价格和实时总市值，历史区间使用实时总股本乘日线收盘价计算市值。

## 功能概览
- 拉取最近一年的日线收盘价并对齐日期。
- 可启用实时行情补齐当天数据：仅在两只股票的实时价格和实时总市值都成功时写入当天记录。
- 页面展示最新实际数据卡片：实时价格、总市值、总股本、实际比例。
- 计算市值序列、比例与折扣：
  - 历史：`zjxc_mv = zjxc_close * zjxc_total_shares`
  - 历史：`xys_mv = xys_close * xys_total_shares`
  - 最新：`zjxc_mv = zjxc_realtime_total_market_cap`
  - 最新：`xys_mv = xys_realtime_total_market_cap`
  - `ratio = xys_mv / zjxc_mv`
  - `discount = ratio * 10`
- 输出 Plotly 交互图，支持“近1周/近1月/近1季/近半年/近1年”区间按钮。
- 页面展示周期统计卡片（最新/最高/最低）并随区间按钮切换。
- 首页有 20 秒缓存；可用 `?refresh=1` 强制刷新。

## 数据源
- 日线数据（按顺序回退）：
  1. 东方财富日线
  2. 新浪日线
- 实时数据（按顺序回退）：
  1. 腾讯实时（价格、总市值、总股本）
  2. 东方财富实时（价格、总市值）

## 关键配置（`app.py`）
- 股票代码：`ZJXC_CODE`、`XYS_CODE`
- 实时开关：`USE_REALTIME`
- 重试参数：`FETCH_RETRY`、`RETRY_DELAY_SECONDS`
- 新浪拉取条数：`SINA_DATALEN`
- 页面缓存：`CACHE_TTL_SECONDS`

## 本地运行
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 7860
```

访问：`http://127.0.0.1:7860/`
