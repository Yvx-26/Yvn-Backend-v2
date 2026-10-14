"""认证接口请求模型。"""

from pydantic import BaseModel


class LoginIn(BaseModel):
    """登录接口接收的用户名和明文密码。"""
    username: str
    password: str
