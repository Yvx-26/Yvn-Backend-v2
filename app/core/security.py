"""登录 token 与密码校验相关逻辑。"""

import hashlib
import secrets

# 这里用内存字典保存登录 token。
# 优点：简单；缺点：服务重启后 token 会失效。
# 进程内登录令牌缓存；服务重启后自动清空。
LOGIN_TOKENS = {}


def sha256_password(password: str) -> str:
    return hashlib.sha256((password or "").encode()).hexdigest()


def create_login_token(user_id, username: str) -> str:
    token = secrets.token_hex(16)
    LOGIN_TOKENS[token] = {"user_id": user_id, "username": username}
    return token


def get_user_by_token(token: str):
    return LOGIN_TOKENS.get(token)


def remove_login_token(token: str):
    LOGIN_TOKENS.pop(token, None)
