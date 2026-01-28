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
