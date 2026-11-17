# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码，见 `app/reasons.py`。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。

## 债权与付款（M7）

### 核心区分

| 概念 | 表 | 含义 | 可变性 |
|---|---|---|---|
| 原始债务 | `invoices` | 发票号、原供应商、发票金额 | 登记后供应商与金额不可改 |
| 收款权（债权） | `claims` | 归属于某一债权人的未付/已付金额 | 归属人固定；金额随转让与付款变动 |
| 转让 | `assignments` | 一次部分转让：转出方 → 受让方 | 只新增，不改写历史 |
| 付款回执 | `payments` | 一次付款的收款人、原供应商、分摊明细 | 只新增，不可改 |
| 验收单 | `receipts` | 真实入库/验收金额，供 M5 匹配 | 单号相同且内容一致则复用 |
| 请求日志 | `requests` / `request_attempts` | 幂等结果与失败原始请求 | 成功结果不可覆盖 |

### 字段

- `invoice_no`：发票号，字符串保形，原始债务主键。
- `supplier_code` / `original_supplier_code`：发票上的供应商编码，始终按字符串存储（前导零保留，TRAP-08）。
- `amount_cents`：金额以整数分为单位存储，避免浮点误差。
- `creditor_code`：债权人编码。供应商与保理商共用该命名空间，编码必须非空。
- `claim_id`：收款权凭证编号。同一发票可有多条 claim，分属不同债权人。
- `paid_cents`：已通过付款回执结清的部分；`unpaid = amount_cents - paid_cents`。
- `origin_assignment_id`：该 claim 由哪次转让产生；原始 claim 为 NULL。
- `version`：乐观并发版本号，每次金额变化 +1，用于条件更新。
- `request_id`：幂等键。同一请求标识与相同内容重试返回原结果；内容不同则返回 `REQUEST_CONFLICT`。
- `expected_balance`：可选前置余额声明，用于付款/转让时拒绝并发失配（不一致返回 `CONCURRENT_MODIFICATION`，且不落账）。
- `parent_assignment_id`：再次转让时指向上游转让；多头来源时为 NULL，完整来源见 `source_claims`。
- `source_claims`：本次转让消耗的转出方 claim 及金额，转让链审计依据。
- `allocations`：付款回执分摊到的 claim 及金额；收款人必须等于该 claim 的债权人。

### 不变量

1. `sum(claims.amount_cents) == invoices.amount_cents`（原始债务守恒）。
2. 每个债权人 `claim = paid + unpaid`，且任一 claim `paid <= amount`。
3. 只有 `unpaid > 0` 且归属转出方的余额可被转让；付款只能付给该债权人的未付余额。
4. 付款回执的 `payee_code` 永远等于付款当时的债权人；转让后不回写。
5. `payments.original_supplier_code == invoices.supplier_code`，即使收款人已是受让方。
6. claim 的归属人永不改变；转让只通过新增子 claim 实现，因此转让链可完整回放。

### 并发与重试

- 付款与转让都以 `BEGIN IMMEDIATE` 开启写事务，并在 `UPDATE ... WHERE version = ? AND amount - paid >= ?` 条件下落账；
  条件不满足即整体回滚并返回 409，不会留下半笔债权或回执。
- 提交前读取的余额只用于生成计划；真正生效由条件更新决定，因此同一余额上并发争用时只有一方成功。
- 失败请求原样写入 `request_attempts`（含原始请求体），通过
  `GET /api/receivables/requests/{request_id}/restore` 恢复后可重试。
- 重试沿用同一 `request_id`：已成功的请求直接返回原回执/转让，不新增债权与付款回执。
