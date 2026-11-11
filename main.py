import threading
import time as _time
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import db
import api

app = FastAPI(title="教学排课管理系统")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api.router)
app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.on_event("startup")
def startup():
    db.init_db()
    # 到期提醒改为发布时一次性预推钉钉待办（dueDate 到点由钉钉自动提醒），无需后台进程。
    # /api/reminders/check 保留为手动触发接口，供需要时使用。
    # 群机器人（Stream 模式）：仅当启用时建立长连接（出站连接，内网可用）
    import dingtalk_bot
    threading.Thread(target=dingtalk_bot.run_forever, daemon=True).start()
