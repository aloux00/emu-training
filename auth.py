import hashlib
import os
import secrets
import jwt
import datetime
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
import db
from sqlmodel import Session, select
import models


def _load_secret() -> str:
    """JWT 签名密钥：优先读环境变量 TEACH_SECRET，否则读本地 secret.key（没有就随机生成并保存）。

    绝不能把密钥写死在代码里——仓库是公开的，密钥泄漏等于任何人都能伪造管理员令牌。
    密钥文件不入 git（见 .gitignore），换机器部署时把 secret.key 一起拷贝即可保持登录态；
    不拷贝也能用，只是所有人需要重新登录一次。
    """
    env = (os.environ.get("TEACH_SECRET") or "").strip()
    if env:
        return env
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret.key")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if len(val) >= 32:
                return val
        val = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(val)
        print("[auth] 已生成新的 JWT 密钥并保存到 secret.key（请勿提交到 git；所有用户需重新登录一次）", flush=True)
        return val
    except Exception as e:
        print("[auth] 无法读取/创建 secret.key（%s），本次运行使用临时密钥，重启后登录态会失效" % e, flush=True)
        return secrets.token_hex(32)


SECRET = _load_secret()
ALGO = "HS256"
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100000).hex()
    return f"{salt}:{h}"


def verify_password(pw: str, stored: str) -> bool:
    if not stored or ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return h == hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100000).hex()


def create_token(user_id: int) -> str:
    # 有效期 7 天：避免隔天打开系统就被判过期（教学系统为单人使用，无需短时效）
    exp = datetime.datetime.utcnow() + datetime.timedelta(days=7)
    return jwt.encode({"sub": str(user_id), "exp": exp}, SECRET, algorithm=ALGO)


def get_current_user(token: str = Depends(oauth2)):
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGO])
        uid = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="无效或过期的令牌")
    with Session(db.engine) as s:
        u = s.get(models.User, uid)
        if not u or not u.is_active:
            raise HTTPException(status_code=401, detail="用户不存在或已禁用")
        return u


def admin_only(user: models.User = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def staff_only(user: models.User = Depends(get_current_user)):
    """教务操作（排课、考核、名单维护）：管理员或教务可用，教师/学生只读。"""
    if user.role not in ("admin", "jiaowu"):
        raise HTTPException(status_code=403, detail="需要管理员或教务权限")
    return user
