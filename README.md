# 采购三方勾稽报告

面向真实数据复核的中型数据工程工具，包含七个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18218。测试：docker compose exec -T app pytest -q

## 模块

- M1 单据采信：页面 /documents，接口 /api/documents/import
- M2 主数据映射：页面 /master，接口 /api/master/resolve
- M3 数量勾稽：页面 /quantity，接口 /api/quantity/match
- M4 税额核验：页面 /tax，接口 /api/tax/check
- M5 多对多匹配：页面 /matching，接口 /api/matches/solve
- M6 差异批次：页面 /batches，接口 /api/reconcile
- M7 债权转让与付款：页面 /receivables，接口 /api/receivables/*（原始债务与收款权分离、部分/再次转让、各债权人余额与回执）

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

债权转让只转移未付收款权，原始发票的供应商与金额冻结不变，历史付款回执保留原收款人。付款与转让带请求标识幂等，失败请求可通过 `GET /api/receivables/requests/{request_id}/restore` 取回原样重试。数据字典见 docs/DATA-DICTIONARY.md。

