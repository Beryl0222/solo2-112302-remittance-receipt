# 侨批银款核账中的上下款归属与分账

合成资料放在 `data/corpus.json`，记录类型载于 `data/context.json`。每条记录有独立的
`id`、业务类别 `kind` 及原始文字 `text`。`arrival_order` 表示收件先后，文字内注明的
业务发生时间可以早于或晚于收件顺序。`references` 只列来源中已明示的记录关联，空值表示
未附明示关联。

`revealed_at` 是研究者在第几步才看到该件，用来按“渐次出现的证据”重排账；它既不是
业务发生时间，也不是收件先后。账页版本也带此字段。

使用 Python 3.11 标准库。`qiaopi` 包在转写之上做分户、分币种、可追查的核账。

## 核账约定

- **封面额不等于家户实收。** “大银一十五元”只证明寄出额；家户收讫须有指向该户的
  签收。信中“内付冬米五元”是用钱意向，“前欠五元照旧”是旧欠，都不自动并入实收。
- **保留笔迹候选。** 十／廿、断行前后的“角／元”、民国年份等候选原样留下；只有
  在有依据时才采用一个值，采用说明写在行的 `amounts[].note`。不为了让账面刚好
  对平而补一个数。
- **“同上”先看栏题。** 回指哪一栏由题头决定；回指经手人时，不把它当金额或汇款人。
- **代领不合并家户。** 同一日同批局由一人代领几家，各户分别记；代领只记保管状态，
  须另有该户签收才计入实收。
- **别名须有住地或本人署名。** 单字“兰”同时可能指两户时，不能只凭别名归户。
- **分币种保留。** 没有明确兑换依据时，不把不同币种折算相加；币种不明就标为
  币种未定，单独列在未决项里。
- **共用收据按指向与签收次序分配。** 一张收据包两封时，先看文字明示的分配，再看
  封标指向，最后才按签收次序；两种分法都说得通时不替人选择，款项停在未分状态。
  已经由另一户签收的金额不会因为某一封“看起来已结”而被挪走。
- **重开一行要回看共用凭据。** 复核判断“内付五元”的对象时，须一并看共用收据的
  其他批号和原来分出的金额；另一家来源独立时，其已入账款项继续有效。
- **旧账页原样保留。** 已刊账页不被改写；当前分账另算，并逐条列出引用所凭分账的
  变化。

## 命令行

    python3 -m qiaopi check                  # 资料与规则自检（结构错误 + 需复核警示）
    python3 -m qiaopi book                   # 全部可见证据的分户摘要
    python3 -m qiaopi book --gate 9          # 只用第 9 步及以前可见的证据
    python3 -m qiaopi show H1                # 一户的逐笔来款、差额、代领与签收次序
    python3 -m qiaopi line L1 L1:2           # 一行的读法、笔迹候选、采用依据、归户依据
    python3 -m qiaopi ledger                 # 旧账页、当前分账与引用变化
    python3 -m qiaopi ledger --page 1923-09/T08
    python3 -m qiaopi reopen                 # 复核重开某行时须回看的共用凭据与已分金额
    python3 -m qiaopi export out/            # 写出 summary/households/unresolved/book.json 与账页

`--gate N` 可放在子命令前后，用来复现“当时只看到这些件”的账。

## 输出

`export` 写出：

- `summary.txt`：分户摘要、代领/保管、未分/未决、读法暂未采用的行；
- `households.csv`：家户 × 币种的实收、封面额、差额（最小单位）；
- `unresolved.csv`：未决款项缺哪项证据、金额候选、指向与原文；
- `book.json`：完整账本、批号明细、签收次序、读法待定、重开影响、当前账页、
  账页对照与引用变化；
- `1923-09_T08.txt`：旧账页原文转写，附当前复核改动、所用记录与复核来源。

金额以辅币“分”为最小单位保存：银元 1 元 = 100 分。`households.csv` 用最小单位，
便于机器比对；文本报告中的金额按币种显示。

## 代码结构

| 模块 | 作用 |
| --- | --- |
| `qiaopi/model.py` | 记录、行、金额、依据的数据模型；中文数字与金额解析 |
| `qiaopi/evidence.py` | 按 `revealed_at` 分层；按住地＋姓名、封标、别名归户；套用复核改写 |
| `qiaopi/allocator.py` | 签收、转交、代领、退回、合封的分账；共用凭据分配；未决原因；重开影响 |
| `qiaopi/ledger.py` | 旧账页原样保留、当前分账对照、引用变化、账页转写 |
| `qiaopi/validate.py` | 结构错误与需复核警示 |
| `qiaopi/report.py` | 文本摘要、CSV、逐户说明、单行解释 |
| `qiaopi/cli.py` | 命令行入口 |

## 资料字段（要点）

记录级：`id`、`kind`、`text`、`arrival_order`、`revealed_at`、`references`、
`authenticity`/`copy_of`/`protocol`、`year_candidates`、`party`/`party_basis`、
`citation`（引用账页）、`revision`（复核改动与 `reopen` 重开行）。

行级（`lines[]`）：`id`、`role`、`text`、`addressee`、`signature`、`currency`、
`amount_candidates`（笔迹候选）、`amounts`（采用值）、`sealed_reference`/`batch`、
`covers`/`allocation`/`pointing`（共用凭据的覆盖范围、明示分配、文本指向）、
`same_as`（“同上”回指的栏）、`genuine_delivery`、`awaiting`、`note`。

行角色见 `qiaopi/model.py` 的 `ROLES`；家户状态见 `allocator._household_status`
与 `report.STATUS_TEXT`。

## 测试

    python3 -m unittest discover -s tests -v
