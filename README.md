---
title: kaypycode
emoji: 📈
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

  # 新易盛 / 中际旭创 市值比例图

  这是一个单页面应用，用于展示两只 A 股的市值比例与折扣趋势图。

  说明：

  - --- 必须紧挨文件开头，不要有空行或空格。
  - emoji 用了常规字符，避免之前的不可见字符导致解析失败。



# Market Cap Ratio (HF Space)

This Space serves a single web page that renders a Plotly chart for the market cap ratio between two A-share stocks.

## Data Sources
- Daily close: AkShare
- Realtime (today only, if daily not updated): Eastmoney

## Run locally
```
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 7860
```
