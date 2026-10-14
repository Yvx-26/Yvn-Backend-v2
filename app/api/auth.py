"""登录、会话校验和注销接口。"""

from typing import Optional
from fastapi import APIRouter, Header, HTTPException

from app.core.security import sha256_password, create_login_token, get_user_by_token, remove_login_token
from app.db.mysql import get_conn
from app.schemas.auth import LoginIn

router = APIRouter()


# 接口 POST /api/login：校验用户名和密码，登录成功后生成临时令牌并返回给前端。
@router.post("/api/login")
def api_login(data: LoginIn):
    """校验账号密码并创建仅在当前后端进程内有效的登录令牌。"""
    conn = None
    try:
        # 清理登录输入并准备与数据库记录进行比较。
        username = (data.username or "").strip()
        # 清理登录输入并准备与数据库记录进行比较。
        password_hash = sha256_password(data.password or "")

        conn = get_conn()
        with conn.cursor() as cursor:
            sql = "SELECT id, username, password_hash FROM admin_user WHERE username=%s LIMIT 1"
            cursor.execute(sql, (username,))
            row = cursor.fetchone()

        if not row or row["password_hash"] != password_hash:
            raise HTTPException(status_code=401, detail="invalid username or password")

        token = create_login_token(row["id"], row["username"])
        return {"ok": True, "token": token, "username": row["username"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# 接口 GET /api/me：解析 Authorization 请求头并校验 Bearer 令牌，返回当前登录用户名。
@router.get("/api/me")
def api_me(authorization: Optional[str] = Header(None)):
    """校验 Bearer 令牌并返回当前登录用户。"""
    try:
        # 校验登录凭证是否存在且格式、状态有效。
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="not logged in")

        token = authorization.split(" ", 1)[1].strip()
        user = get_user_by_token(token)
        if not user:
            raise HTTPException(status_code=401, detail="invalid token")

        return {"ok": True, "username": user["username"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 接口 POST /api/logout：解析 Bearer 令牌并将其从内存令牌表删除。
@router.post("/api/logout")
def api_logout(authorization: Optional[str] = Header(None)):
    """删除当前 Bearer 令牌；重复注销也按成功处理。"""
    try:
        # 校验登录凭证是否存在且格式、状态有效。
        if authorization and authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1].strip()
            remove_login_token(token)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
