---
title: kaypycode
emoji: 📈
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# 新易盛 / 中际旭创 市值比例图

这是一个基于 FastAPI 的 Hugging Face Docker Space，提供单页可视化，展示两只 A 股在最近一年内的市值比例与折扣（`ratio * 10`）。

## 功能概览
- 拉取最近一年的日线收盘价并对齐日期。
- 可启用实时行情补齐当天数据：任一股票实时价成功都会写入当天记录，另一只使用最新日线收盘价兜底。
- 计算市值序列、比例与折扣：
  - `zjxc_mv = zjxc_close * zjxc_shares`
  - `xys_mv = xys_close * xys_shares`
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
  1. 东方财富实时
  2. 新浪实时
  3. 腾讯实时

## 关键配置（`app.py`）
- 股票代码：`ZJXC_CODE`、`XYS_CODE`
- 市值锚点（亿元）：`ZJXC_MARKET_CAP`、`XYS_MARKET_CAP`
- 显式股本（亿股）：`ZJXC_SHARES`、`XYS_SHARES`
  - 仅当两者都非 `None` 时，才覆盖市值锚点反推逻辑
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
