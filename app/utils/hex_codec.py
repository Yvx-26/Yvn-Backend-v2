"""共享 Hex 文本清理；严格解析与历史回退策略由各调用方决定。"""

def compact_hex_text(value) -> str:
    return (
        str(value or "").strip()
        .replace(" ", "").replace("\n", "").replace("\r", "").replace("\t", "")
    )
