#!/usr/bin/env python3
"""Regenerate 《源代码.txt》 for the CPCC (software copyright) dossier.

Reproduces the exact submission format verified across sibling dossiers:

* a cover block on page one naming the developer (许达 / 中国移动研究院);
* every kgov + tests source file concatenated in a stable order, each under
  a ``# ===== File: <path> =====`` marker;
* blank lines and comment-only lines are stripped (CPCC tallies "lines
  excluding comments and blanks");
* the stream is cut to the CPCC convention of front 30 + back 30 pages
  (60 pages x 50 lines = 3000 lines);
* each page carries the running header ``Kafka-Govern V1.0`` and a footer
  ``第 N 页``.

Run from the repository root:

    python3 kgov-软件著作权申请/gen_source.py

Writes ``kgov-软件著作权申请/源代码.txt`` and prints the exact tally quoted
by the other dossier documents.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "源代码.txt"

HEADER = "Kafka-Govern V1.0"
PAGE_TOTAL_LINES = 50
PAGES = 60
FRONT_PAGES = PAGES // 2

COVER = [
    "Kafka-Govern V1.0 源代码（按 A4 纵向、宋体五号、每页 50 行排版）",
    "=" * 70,
    "软件名称：Kafka-Govern（简称：kgov）    版本号：V1.0",
    "著作权人/申请人：许达    开发单位：中国移动研究院",
    "源程序总量：有效代码约 7860 行（去除空行与注释行后统计，其中 kgov 主程序",
    "约 2708 行、tests 测试代码约 5152 行）；本申请共提交 60 页。",
    "排版说明：每页第一行为页眉『Kafka-Govern V1.0』（左对齐），其下为 50 行源代码，",
    "最后一行为页脚页码。请使用 A4 纵向纸、正文宋体五号（10.5 磅）、",
    "页边距上下 2.54cm 打印。源代码按模块顺序取前 30 页 + 后 30 页的可识别",
    "程序片段，模块总体设计详见《软件设计说明书》。",
    "=" * 70,
]

# Stable order: core modules first (as in the docs' per-module tally),
# then tests, mirroring the original dossier layout.
FILES = [
    "kgov/__init__.py",
    "kgov/model.py",
    "kgov/rules_errors.py",
    "kgov/broker.py",
    "kgov/mock_registry.py",
    "kgov/registry.py",
    "kgov/report.py",
    "kgov/rules.py",
    "kgov/cli.py",
    "kgov/schema_diff.py",
    "tests/conftest.py",
    "tests/test_broker.py",
    "tests/test_cli.py",
    "tests/test_fuzz_probe.py",
    "tests/test_registry.py",
    "tests/test_report.py",
    "tests/test_rules.py",
    "tests/test_schema_diff.py",
]


def content_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() != "" and not line.lstrip().startswith("#")
    ]


def tally() -> tuple[dict[str, int], dict[str, int]]:
    core: dict[str, int] = {}
    tests: dict[str, int] = {}
    for name in FILES:
        if name.startswith("kgov/"):
            core[name[len("kgov/") :]] = len(content_lines(REPO / name))
        else:
            tests[name[len("tests/") :]] = len(content_lines(REPO / name))
    return core, tests


def build() -> str:
    flow: list[str] = []
    for name in FILES:
        flow.append(f"# ===== File: {name} =====")
        flow.extend(content_lines(REPO / name))
        flow.append("")

    target = PAGES * PAGE_TOTAL_LINES
    if len(flow) > target:
        half = target // 2
        flow = flow[:half] + flow[len(flow) - half :]  # front 30 + back 30
    while len(flow) < target:
        flow.append("")
    flow = flow[:target]

    pages = [
        flow[i * PAGE_TOTAL_LINES : (i + 1) * PAGE_TOTAL_LINES]
        for i in range(PAGES)
    ]
    out: list[str] = list(COVER) + [""]
    for n, page in enumerate(pages, start=1):
        out.append(HEADER)
        out.extend(page)
        out.append(f"第 {n} 页")
    return "\n".join(out).rstrip("\n") + "\n"


def main() -> int:
    body_text = build()
    OUT.write_text(body_text, encoding="utf-8")
    pages = len(re.findall(r"^第 \d+ 页$", body_text, re.M))
    core, tests = tally()
    csum = sum(core.values())
    tsum = sum(tests.values())
    print(f"wrote {OUT} ({pages} pages, {len(body_text.splitlines())} lines)")
    print("core modules (lines excluding comments/blanks):")
    for k in sorted(core, key=lambda x: core[x]):
        print(f"  {core[k]:5d}  kgov/{k}")
    print(f"kgov total: {csum} | tests total: {tsum} | grand total: {csum + tsum}")
    print(
        "physical lines: kgov %d | tests %d"
        % (
            sum(1 for p in Path("kgov").glob("*.py") for _ in p.open()),
            sum(1 for p in Path("tests").glob("*.py") for _ in p.open()),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
