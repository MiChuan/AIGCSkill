# 示例：一次提交审查的完整走查

这份示例演示**模式 6 → 模式 7** 的完整链路：从 `git show` 到一份 HTML 报告。

- 输入：`examples/review-demo.json`（结构化结论）
- 产物：`examples/review-demo.html`（单文件，双击可开）
- 复现：`python scripts/build_report.py --spec examples/review-demo.json --out out/review.html --open`

## 这个案例假定发生了什么

```
$ git log -1 --stat
commit 3f9c2a1  feat(orders): 批量创建订单
 app/api/orders.py                |  41 ++++++
 app/services/order_service.py    | 128 ++++++++++++++++
 app/repo/order_repo.py           |  19 ++-
 tests/test_order_batch.py        |  64 +++++++++
 requirements.txt                 |   1 +
 ...
```

走查顺序不是「按文件读」，是**按数据流读**：

| 步 | 动作 | 得到什么 |
| --- | --- | --- |
| 1 | 读 diff，标出用户输入进入的位置 | `orders.py:84` 的 `ids` query 参数 |
| 2 | 顺着它往下走：解析 → 校验 → 拼 SQL → 执行 → 序列化 | 第 2 步没有校验，第 3 步直接拼字符串 |
| 3 | 顺着同一个入口看异常路径 | `order_service.py:134` 循环 commit；`:170` 吞掉 rollback 异常 |
| 4 | 看未改动但被调用的代码 | `order_repo.py:42` 的惰性加载 = N+1 |
| 5 | 逐条给出触发条件，能写出来的留下，写不出的删掉 | 5 条有效发现 |

## 为什么是这五个标签页

报告的结构对应**读者会问的问题**，不是我的检查顺序：

| 标签页 | 回答的问题 |
| --- | --- |
| 结论 | 能不能合入？最严重的是哪条？ |
| 安全 | 外部能不能打进来？ |
| 逻辑 | 数据会不会写错？ |
| 性能 | 量涨了会不会垮？ |
| 已检查 | 你到底看了什么？有没有漏 |

「已检查」这一页经常被省掉，但它决定这份报告可不可信——只列问题不列范围，读者无法判断你是查遍了还是只看了 diff。

## 流程图用在哪

结论页那张图是**根因链**，不是请求流程：

```
GET /orders/batch  →  split 后拼字符串  →  execute(f"... IN ({ids})")  →  UNION 读出 users 表  →  原样返回
```

每两个节点之间的 `via` 标签写的是「这一步什么变了」——`无类型校验`、`字符串进 SQL`、`任意 SQL 片段`、`泄露`。这条链能读通，根因就成立；读不通，说明中间有一环没确认。

## 三个可以直接抄的判断

1. **`psycopg2` 不允许一次执行多条语句，所以 `DROP TABLE` 不成立，但 `UNION SELECT` 成立。** 审查安全问题时，不能停在「这里拼了 SQL，很危险」，要说清**在当前这个驱动/配置下，攻击者实际能做到什么**。
2. **`19.99 * 3 = 59.970000000000006` 写库时被截断成 59.97 没问题，但对账脚本读原始 float 会累出差额。** 同一个精度问题，在不同下游有不同的后果，要指认具体那个下游。
3. **N+1 的严重度取决于数据量，不取决于代码形状。** `page_size=100` 时 101 次往返、p99 从 40ms 到 780ms——有数字才有优先级。没测就给「另开 PR」。

## 换成你自己的项目

```bash
# 1. 取 diff
git show HEAD --format=fuller > /tmp/diff.txt

# 2. 按 references/06-review.md 的三张表逐条过，写成 JSON
#    结构照抄 review-demo.json，把 items 换成你的发现
#    没有发现就把那一页写成「已检查」的证据

# 3. 校验 + 生成 + 打开
python scripts/build_report.py --spec my-review.json --check
python scripts/build_report.py --spec my-review.json --out out/my-review.html --open

# 4. 聊天里只回三五行：
#    「3 条必修（1 个 SQL 注入），报告：out/my-review.html」
```

JSON 里最容易写错的两处：`tone` 只能取 `bad/warn/note/ok/info`；表格 `rows` 的列数必须等于 `head` 的长度（不等会直接报错，不会悄悄错位）。
