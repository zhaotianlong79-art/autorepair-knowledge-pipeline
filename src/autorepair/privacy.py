"""Conservative demo PII detection. Match contact context, never arbitrary part digits."""

import re

PHONE = re.compile(r"(?i)((?:电话|手机|phone|mobile)\s*[:：]?\s*)(1[3-9]\d{9})(?!\d)")
PLATE = re.compile(
    r"((?:车牌|plate)\s*[:：]?\s*)([京沪粤苏浙川渝鲁鄂湘皖闽赣豫晋陕辽吉黑冀贵云桂琼甘宁青藏新津蒙][A-Z][A-Z0-9]{5,6})"
)


def redact(text: str) -> str:
    text = PHONE.sub(lambda m: m[1] + "[PHONE]", text)
    return PLATE.sub(lambda m: m[1] + "[PLATE]", text)
