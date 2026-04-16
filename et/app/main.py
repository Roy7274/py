# app/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from app.router.transaction_router import router,stats_router

# 创建 FastAPI 实例（类似 SpringBootApplication 启动类）
app = FastAPI(title="个人记账 API", version="1.0")

# 🌟 跨域配置（非常重要！否则 React 无法请求 Python）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有前端来源（开发阶段用），生产环境需指定域名
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有请求头
)

app.include_router(router)
app.include_router(stats_router)