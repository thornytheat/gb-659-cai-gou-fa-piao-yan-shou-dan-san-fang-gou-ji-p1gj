# 深度登记 采购三方勾稽报告

- 规模：中型
- 核心规则：采购、分批验收和合并发票按货号与税率分组，数量和金额使用 Decimal 守恒。
- 使用者与工作流：应付会计导入三类单据，映射主数据，核对数量税额，多对多匹配并导出差异；
  发票债权转让后仍需按原供应商复核三方勾稽，并分别向各债权人付款。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 单据采信 | 导入三类单据并隔离冲突 | POST /api/documents/import | `app/modules/m1_documents/parser.py` `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py` |
| M2 主数据映射 | 归一货号与旧字段 | POST /api/master/resolve | `app/modules/m2_master/aliases.py` `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py` |
| M3 数量勾稽 | 统一包装单位并核对未到量 | POST /api/quantity/match | `app/modules/m3_quantity/units.py` `app/modules/m3_quantity/match.py` | `tests/test_m3_quantity.py` |
| M4 税额核验 | 重算净额、税额与含税额 | POST /api/tax/check | `app/modules/m4_tax/calculate.py` `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py` |
| M5 多对多匹配 | 在相同货号税率内组合匹配真实发票与验收 | POST /api/matches/solve | `app/modules/m5_matching/candidates.py` `app/modules/m5_matching/solver.py` `app/modules/m5_matching/receivables_view.py` | `tests/test_m5_matching.py` `tests/test_m7_api.py` |
| M6 差异批次 | 核对金额守恒并导出差异 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 债权转让与付款 | 分离原始债务与收款权，部分/再次转让，向各债权人付款 | POST /api/receivables/assignments、/payments | `app/modules/m7_receivables/ledger.py` `app/modules/m7_receivables/money.py` | `tests/test_m7_receivables.py` `tests/test_m7_api.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-05 | 单据缺少字段 | 静默补空进入匹配 | 隔离并保留行号 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-06 | 金额带千分位 | 直接转换失败 | 清理分隔符后使用 Decimal | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_thousands_amount_uses_decimal` |
| TRAP-07 | 空值哨兵 | 被当作零金额 | 空值集合统一拒绝 | `app/modules/m1_documents/money.py` | `tests/test_m1_documents.py::test_missing_field_quarantined` |
| TRAP-08 | 供应商编码前导零 | 转整数后主数据失配 | 编码始终按字符串保形 | `app/modules/m2_master/aliases.py` | `tests/test_m2_master.py::test_supplier_code_preserves_leading_zero` |
| TRAP-10 | 税额浮点尾差 | 分项和总额差一分 | Decimal 半入到分 | `app/modules/m4_tax/calculate.py` | `tests/test_m4_tax.py::test_half_up_to_cent` |
| TRAP-14 | 重复单号值冲突 | 后写覆盖金额 | 单独隔离冲突版本 | `app/modules/m1_documents/parser.py` | `tests/test_m1_documents.py::test_conflicting_duplicate_detected` |
| TRAP-17 | 旧列名仍在使用 | 新版读取报错 | 受控字段迁移并报告冲突 | `app/modules/m2_master/evolve.py` | `tests/test_m2_master.py::test_old_columns_migrate` |
| TRAP-19 | 组合候选顺序变化 | 匹配结果随输入顺序变化 | 按差额、数量与编号稳定择一 | `app/modules/m5_matching/solver.py` | `tests/test_m5_matching.py::test_choice_is_order_independent` |
| TRAP-23 | 相邻税率边界 | 不同税率金额互相抵消 | 货号与税率共同分组 | `app/modules/m4_tax/verify.py` | `tests/test_m4_tax.py::test_different_tax_rates_never_offset` |
| TRAP-26 | 同批重复执行 | 重复新增对账结果 | 输入摘要作为运行唯一键 | `app/store.py` | `tests/test_m6_batches.py::test_idempotent_record` |
| TRAP-27 | 转让后改发票供应商 | 三方勾稽与原供应商断开 | 原始债务冻结，收款权另立 claim | `app/modules/m7_receivables/ledger.py` | `tests/test_m7_receivables.py::test_invoice_supplier_cannot_be_changed` |
| TRAP-28 | 转让已付部分 | 同一笔余额被转让又被付款 | 只按 `unpaid` 计划转让，转让额受条件更新约束 | `app/modules/m7_receivables/ledger.py` | `tests/test_m7_receivables.py::test_assignment_of_paid_portion_rejected` |
| TRAP-29 | 付款回执跟着新债权人 | 历史回执收款人被改写 | 回执不可变，记录付款当时收款人与原供应商 | `app/modules/m7_receivables/ledger.py` | `tests/test_m7_receivables.py::test_payment_to_original_supplier_keeps_receipt_payee_after_assignment` |
| TRAP-30 | 付款与转让并发 | 双方都按旧余额落账 | 版本条件更新 + 行事务，一方回滚 | `app/modules/m7_receivables/ledger.py` | `tests/test_m7_receivables.py::test_concurrent_payment_and_assignment_only_untouched_balance_wins` |
| TRAP-31 | 落账失败后重试 | 新增第二笔债权或回执 | request_id 幂等，失败请求可恢复原样重试 | `app/modules/m7_receivables/ledger.py` | `tests/test_m7_receivables.py::test_failed_request_can_be_restored_and_retried_without_side_effects` |

## 对账规则

- 采购数量 = 净验收数量 + 未到数量。
- 净验收含税额 = 已开票含税额 + 未开票金额 + 已解释差异。
- 不同税率的行不可相互抵消。
- 发票金额 = 全部债权人已付款 + 全部债权人未付余额；原始供应商与发票金额在转让前后保持不变。

## 已知不覆盖

- 不处理外币汇兑，样例与输出均为人民币。
- 不扩展银行打款通道：本模块只登记付款回执与债权余额，不发起实际转账。
- 商品匹配算法（`candidates.py` / `solver.py`）保持不变，匹配页仅接入真实发票、转让与付款数据。
