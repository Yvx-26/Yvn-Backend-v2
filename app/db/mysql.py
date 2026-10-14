"""MySQL 连接工具。"""

from app.core.config import db_config


def get_conn():
    """创建一个新的 MySQL 连接。每个接口用完后需要 close。"""
    # 延迟导入让不访问数据库的模型/趋势单元测试无需安装 MySQL 驱动。
    import pymysql
    from pymysql.cursors import DictCursor

    return pymysql.connect(**db_config, cursorclass=DictCursor)
