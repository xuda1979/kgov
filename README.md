# kgov — Kafka-Govern

Kafka topic 治理助手（schema diff / 脏数据规则 / 治理报告）。自托管、离线可用，
为中小团队的 Kafka + Schema Registry 提供轻量治理：检测 schema 演进兼容性、
扫描 topic 脏数据、生成含 owner / 陈旧 / 违规计数的治理报告。

面向 Confluent 的轻切片：不依赖 Confluent 云，全部通过 fixture 文件离线运行
（也可扩展接入真实 broker）。

## 功能

- **Schema diff**：比较两个 Avro / JSON Schema，标记 `Breaking` / `Non-Breaking` /
  `Additive`，输出升级指引（Markdown）或 JSON（兼容性 matrix）。schema 来源可为
  文件，也可为 Schema Registry URL / `registry-url/subject/version`。
- **脏数据规则 DSL**（YAML/JSON）：字段 `type`、`required`、`enum`、`pattern`、
  `range`、`non_null` 校验；对已入队消息执行规则，输出违规样本
  （offset / key / field / failed check）。
- **治理报告**：topic 清单——owner（tag）、schema 状态、陈旧度（无消费）、
  违规计数；可导出 CSV / JSON，适合 cron 定时生成。
- **Schema Registry 客户端**：`kgov registry --registry-url` 直接拉取 subject /
  schema / compatibility，离线 fixture 或在线注册表皆可；本地 `MockRegistry`
  可在无注册表时提供 Confluent wire 端点。
- **真实 broker（P3，可选）**：`kgov topics --bootstrap` 拉取 topic 元数据、
  `kgov check --broker --topic` 直接扫描线上消息；以 `confluent-kafka`（可选依赖）
  实现，离线核心不受影响。
- **CI/CLI**：`kgov check --config rules.yaml`，退出码作为门禁。

## 安装

要求 Python ≥ 3.11，核心依赖 `avro`、`jsonschema`、`PyYAML`。

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .            # 或 pip install -e ".[dev]" 以安装 pytest
```

连接真实 broker 需要 `confluent-kafka`（可选）：`pip install -e ".[broker]"`。

## 用法

### schema-diff

```bash
kgov schema-diff --before v1.json --after v2.json [--format avro|json] \
    [--subject payments-value] [-o diff.md|diff.json]
```

也可以直接对比 Schema Registry 中两个版本的 schema：

```bash
kgov schema-diff \
    --before http://registry:8081/subjects/payments-value/versions/1 \
    --after  http://registry:8081/subjects/payments-value/versions/2
```

输出 Markdown 变更清单；`--json` 或 `.json` 后缀输出 JSON。整体兼容性为
Breaking 时退出码 1（`rec.check` 门禁）。

### check（脏数据规则 / CI 门禁）

```yaml
# rules.yaml
version: 1
topic: payments
name: payments-constraints
rules:
  - name: payments-constraints
    fields:
      payment_id:
        type: string
        required: true
        pattern: "^pay_"
      amount_cents:
        type: integer
        required: true
        non_null: true
        range: {minimum: 0, maximum: 1000000}
      currency:
        type: string
        pattern: "^[A-Z]{3}$"
      status:
        type: string
        enum: [ACCEPTED, PENDING, REJECTED]
```

```bash
kgov check --config rules.yaml --messages messages.json \
    [--max-violations N] [-o violations.json]
```

- 退出码 `0` = 无违规（或在门禁内）、`1` = 发现违规（或超门禁）、
  `2` = 用法/配置错误、`3` = 运行时错误。
- `--messages` 指向 JSON fixture：`{"topic": "payments", "messages": [{"offset": 0, "key": "k", "value": {...}}]}`。
- **必须提供数据源**（`--messages` 或 `--broker --topic`）：`check` 在没有数据时
  报错（退出码 3），避免 CI 门禁因空扫描静默通过。
- 连接真实 broker 直接扫描线上消息：`kgov check --config rules.yaml --broker host:port --topic payments`
  （需要 `pip install -e ".[broker]"`）。
- 线上消息若为 Confluent-wire Avro（`0x00` magic + schema id + Avro binary），
  加 `--schema-registry http://localhost:8081` 让 `check` 按注册表里的 schema 解码
  后再执行规则；解码失败或非 Avro 值自动回退为原始文本 / hex，不会中断扫描。

### registry（Schema Registry 客户端）

```bash
kgov registry --registry-url http://localhost:8081 [-o registry.json]
kgov registry --registry-url http://localhost:8081 --subject payments-value
```

无 `--subject` 时导出全量 registry fixture：`{"subjects": {...}, "generated_at": ...}`；
指定 subject 时输出该 subject 各版本的 id/version/compatibility/schema。

### topics（broker 元数据）

```bash
kgov topics --bootstrap localhost:9092 [-o topics.json]
```

拉取 topic 名称、分区数、副本因子（需要 `kgov[broker]`）。

### report（治理报告）

```bash
kgov report --topics topics.json \
    [--registry registry.json] \
    [--groups consumer_groups.json] \
    [--rules rules.yaml --messages messages.json | --violations violations.json] \
    [--format json|csv] [-o report.json|report.csv]
```

每行一个 topic：`name, owner, tags, partitions, replication_factor,
schema_subject, schema_version, schema_state, latest_message_at,
last_consumed_at, stale, violation_count`。

fixture 输入格式：

- `topics.json`：`{"topics": [{"name", "partitions", "replication_factor",
  "owner", "tags[]", "schema_subject", "latest_message_at"}]}`
- `registry.json`：`{"subjects": {"<subject>": [{"id", "version",
  "compatibility", "schema"}, ...]}}`（兼容 Confluent wire 形式）
- `consumer_groups.json`：`{"groups": {"<topic>": {"topic", "group",
  "last_consumed_at", "consumer_count"}}}`

陈旧度（stale）判定：topic 有消费组时看 `last_consumed_at` 距今是否超过
阈值（默认 3 天）；无消费组时看最近消息时间（默认 2 天）。

### export

```bash
kgov export --type report --topics topics.json --format csv -o report.csv
```

## 离线模式

所有命令在无 broker / 无注册表情况下全功能可用：数据均来自 JSON/YAML fixture
（`kgov report`、`kgov check --messages`、`kgov schema-diff` 文件模式）。
`MockRegistry`（`kgov.mock_registry`）在本地 127.0.0.1 起一个只读的 Confluent
wire Schema Registry，供 `kgov registry` / 集成测试离线使用真实的 HTTP 往返。

## 示例（离线数据集）

`examples/` 提供一套连贯的离线示例数据，覆盖全部子命令的输入格式，无需
真实 broker / Schema Registry 即可端到端体验：

```text
examples/
├── schemas/                # Avro schema：payment_v1 / payment_v2（兼容演进）、
│                           #   payment_breaking（不兼容演进）
├── rules/rules.yaml        # payments topic 脏数据规则
├── messages/               # payments_messages.json：6 条含脏数据的消息
├── topics/topics.json      # broker 元数据快照
├── registry/registry.json  # Schema Registry 快照（subjects / versions / 兼容性）
└── consumer_groups/        # 消费组快照
```

用 `python -m kgov.cli`（或安装 console script 后的 `kgov`）直接体验：

```bash
# schema-diff：v1 → v2 为兼容演进（Additive，退出码 0）
#              v1 → payment_breaking 为不兼容（Breaking，退出码 1）
python -m kgov.cli schema-diff --before examples/schemas/payment_v1.json \
    --after examples/schemas/payment_v2.json --subject payments-value
python -m kgov.cli schema-diff --before examples/schemas/payment_v1.json \
    --after examples/schemas/payment_breaking.json --subject payments-value

# check：对 6 条消息执行规则，检出 8 处违规，门禁失败（退出码 1）
python -m kgov.cli check --config examples/rules/rules.yaml \
    --messages examples/messages/payments_messages.json

# report：三 topic 治理报告（schema 状态 / 陈旧度 / 违规计数）
python -m kgov.cli report --topics examples/topics/topics.json \
    --registry examples/registry/registry.json \
    --groups examples/consumer_groups/consumer_groups.json \
    --rules examples/rules/rules.yaml \
    --messages examples/messages/payments_messages.json

# export：把治理报告导出为 CSV
python -m kgov.cli export --type report --topics examples/topics/topics.json \
    --registry examples/registry/registry.json \
    --groups examples/consumer_groups/consumer_groups.json \
    --format csv -o report.csv
```

`registry` 命令可连本地 MockRegistry（`kgov.mock_registry`，Confluent wire 端点）
离线跑全量 / 单 subject 拉取；`topics` 需要真实 broker（`kgov[broker]`），
不可达时按退出码约定以码 3 报错。

## 开发

```bash
python -m pytest            # 全部测试（P0 diff / P1 rules / P2 report / P3 registry·broker·CLI）
```

连接真实 broker 的集成测试默认跳过，设置环境变量后运行：

```bash
KGOV_LIVE_BROKER=localhost:9092 python -m pytest tests/test_broker.py
```

测试与 fixture：

- `tests/fixtures/schema_evolution/` — Avro / JSON Schema 演进系列
  （bi/兼容分类样本）
- `tests/fixtures/rules/`、`tests/fixtures/messages/` — 脏数据规则与违规消息
- `tests/fixtures/topics/`、`tests/fixtures/subject_groups/`、`tests/fixtures/registry/`
  — 治理报告输入
- `tests/fixtures/schemas/` — schema-diff CLI 测试所用的独立 schema 文件（v1/v2、
  breaking before/after）

## 退出码约定

| 码 | 含义 |
|----|------|
| 0 | 通过（无违规/兼容；或输出成功） |
| 1 | 门禁失败（违规、Breaking schema diff） |
| 2 | 用法/配置错误（argparse / CLI 校验） |
| 3 | 运行时错误（文件缺失、解析失败） |

## 授权与协议

双授权：源代码以 **GNU AGPL v3.0** 开源（见 [`LICENSE`](LICENSE)）。任何
商业或营利性使用，或将其用于向他人销售/授权的产品与服务，均须事先获得
著作权人（许达，中国移动研究院）的书面商业授权，详见
[`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md)。
