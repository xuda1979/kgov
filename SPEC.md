# 13 — Kafka-Govern: Kafka topic 治理助手（schema diff / 脏数据规则）

## 定位
Confluent（ARR $1.2B）的轻切片：为中小团队 Kafka/Schema Registry 提供 topic 治理
助手——schema diff、脏数据规则扫描、演进报告。自托管，不开 Confluent 云。

## 主要痛点
- topic 无主、无 schema 版本演进，下游被 Breaking change 打断
- 脏数据（格式错、字段类型不匹配）进 topic 污染
- 团队想要轻量的治理报告（谁在用、schema 变了什么）而不是企业平台

## 功能需求
1. 连接（可选本地 mock）：Schema Registry（兼容 Confluent wire）+ broker metadata
   - 离线模式：支持 `kafka-configs`/文件导出，或纯 fixture 测试
2. Schema diff：
   - 比较两个 schema（Avro/JSON Schema），标记 Breaking / Non-Breaking / Additive
   - 输出到 Markdown / JSON（升级指引、兼容性 matrix）
3. 脏数据规则：
   - 规则 DSL（YAML/JSON）：字段类型、required、enum、pattern、范围、null 禁止
   - 校验入队数据（读取已存在消息执行规则），输出违规样本（offset/key/failed rule）
4. 治理报告：
   - topic 清单：owner（tag）、schema 状态、陈旧（无消费）、违规计数
   - 可导 CSV/JSON、可由 cron 定时生成
5. CI/cli：`kgov check --config rules.yaml`

## 非目标
- 完整 Kafka 平台/控制台（Confluent Control Center 替代）
- 自动迁移数据

## 技术栈
- Python 3.14（avro / jsonschema）
- confluent-kafka-python（可选，仅在需要读真实 broker 时）
- fixture 模拟 registry（json 文件）
- 测试：schema 演进 fixture（bi/兼容系列）

## 低算力守则
见全局（本地 diff/校验都是轻活）。

## AC
1. 对给定两个 schema（含有 Breaking 案例），正确判定兼容性并解释
2. 脏数据规则的 5 个样例违规消息正确检报（含 offset 定位）
3. 治理报告包含：total topics、owner、stale、violation count（基于 fixture）
4. 离线模式（无 broker）全功能可用
5. `check` 退出码正确（门禁）
6. CSV/JSON 导出
7. 测试 + README + fixture 数据

## Phases
- P0: schema diff 引擎
- P1: 规则引擎 + 校验
- P2: 治理报告 + 导出
- P3: 真实 broker 连接（可选）

## 里程碑
M1: schema diff（MVP）
M2: 脏数据规则
M3: 治理报告

## 变现
- CLI 免费，团队治理仪表板订阅
