# 专项资金分摊与舍入尾差复核

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 预算采信：`/api/budgets/import`
- M2 资格校验：`/api/eligibility/evaluate`
- M3 权重测算：`/api/weights/calculate`
- M4 金额分摊：`/api/allocations/calculate`
- M5 尾差裁决：`/api/remainders/resolve`
- M6 批次复核：`/api/export/allocations.csv`
- M7 复核合并：`/api/review/merge`

        ## 核心规则

        先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。

        ## 复核合并

        多人离线复核各自署名提交意见（`POST /api/review/opinions`，按令牌幂等收件，重试不重复入库）。合并按字段进行：不同字段的更正各自吸收，同字段同值自动采信，同字段分歧挂争议并保留基准值，由裁决绑定已见意见序号定夺；裁决后新增同字段意见使旧裁决自动失效。争议项目暂停参与权重，其余项目继续测算。合并结果与权重在同一事务生效，失败保留上一版本，中断重开后从已提交状态恢复。`GET /api/review/status` 回读原意见（含其所见基准原值）、争议、采信来源与权重来源。本模块复用 M2 资格规则与 M3 权重归一，不改动其规则，不连接拨款。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18213`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
