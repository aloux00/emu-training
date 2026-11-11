import hashlib
import secrets
import jwt
import datetime
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
import db
from sqlmodel import Session, select
import models

SECRET = "dev-secret-change-me"
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
