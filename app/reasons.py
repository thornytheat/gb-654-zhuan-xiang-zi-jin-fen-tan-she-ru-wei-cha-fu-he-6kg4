REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "UNKNOWN_BASELINE": "引用的基准版本未登记", "NO_OPINIONS": "该字段尚无意见可裁",
    "NEGATIVE_WEIGHT": "权重为负", "ZERO_WEIGHT_GROUP": "有效权重合计为零",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
