"""兼容旧启动方式的入口文件。

仍然可以使用：
    python main.py
也可以使用：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import uvicorn
from app.main import app


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
