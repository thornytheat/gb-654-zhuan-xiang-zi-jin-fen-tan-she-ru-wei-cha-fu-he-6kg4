# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |
| `token` | string | 否 | 意见收件幂等令牌，重试凭此返回原收件 |
| `base_version` | string | 否 | 意见所依据的基准版本，基准快照随库保存 |
| `base_value` | any | 是 | 回读时该意见在其基准版本下所见的字段原值 |
| `seen_seq` | integer | 否 | 裁决绑定的已见意见最大序号，之后新意见使裁决失效 |
| `weight_source` | string | 否 | 权重取值来源：baseline / opinion:署名 / adjudication:署名 / default |
| `dirty` | boolean | 否 | 当前意见状态与最近合并版本不一致，待重新合并 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
