from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
import pandas as pd
import numpy as np
from io import StringIO
import json

app = FastAPI(title="KayPyCode", description="数据分析工具 API")


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>KayPyCode - 数据分析工具</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
            h1 { color: #333; }
            .endpoint { background: #f5f5f5; padding: 10px; margin: 10px 0; border-radius: 5px; }
            code { background: #e0e0e0; padding: 2px 6px; border-radius: 3px; }
        </style>
    </head>
    <body>
        <h1>📊 KayPyCode - 数据分析工具</h1>
        <p>欢迎使用数据分析 API 服务！</p>

        <h2>可用接口</h2>
        <div class="endpoint">
            <strong>GET /health</strong> - 健康检查
        </div>
        <div class="endpoint">
            <strong>POST /analyze</strong> - 上传 CSV 文件进行数据分析
        </div>
        <div class="endpoint">
            <strong>POST /statistics</strong> - 计算基础统计信息
        </div>
        <div class="endpoint">
            <strong>GET /docs</strong> - API 文档 (Swagger UI)
        </div>
    </body>
    </html>
    """


@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "kaypycode"}


@app.post("/analyze")
async def analyze_csv(file: UploadFile = File(...)):
    """上传 CSV 文件并返回基础分析结果"""
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="请上传 CSV 文件")

    try:
        contents = await file.read()
        df = pd.read_csv(StringIO(contents.decode('utf-8')))

        analysis = {
            "filename": file.filename,
            "rows": len(df),
            "columns": len(df.columns),
            "column_names": df.columns.tolist(),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
            "missing_values": df.isnull().sum().to_dict(),
            "preview": df.head(5).to_dict(orient='records')
        }
        return analysis
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")


@app.post("/statistics")
async def calculate_statistics(data: dict):
    """计算数值列表的基础统计信息"""
    if "values" not in data:
        raise HTTPException(status_code=400, detail="请提供 'values' 字段")

    values = data["values"]
    if not isinstance(values, list) or len(values) == 0:
        raise HTTPException(status_code=400, detail="'values' 必须是非空数组")

    try:
        arr = np.array(values, dtype=float)
        return {
            "count": len(arr),
            "mean": float(np.mean(arr)),
            "std": float(np.std(arr)),
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "median": float(np.median(arr)),
            "q25": float(np.percentile(arr, 25)),
            "q75": float(np.percentile(arr, 75))
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"计算失败: {str(e)}")
