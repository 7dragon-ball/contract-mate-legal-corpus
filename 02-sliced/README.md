# 02-sliced：切片产物（压缩包）

本目录只放压缩包，不保留展开目录。**包内路径相对批次根**，解压到同一个批次目录即可复原目录树。

## 1. 解压复原

```bash
cd 02-sliced
mkdir -p preprocess-first-batch-20261105
for z in *.zip; do unzip -q "$z" -d preprocess-first-batch-20261105; done
```

复原后：

```text
preprocess-first-batch-20261105/
├── hk/<法例>/<语言>/<版本>/     7 文件（en / zh-Hant）
├── sg/<法例>/en/<版本>/         7 文件 + raw-pages.jsonl
└── cn/<法名>/zh/<版本>/         7 文件，外加 cn/索引_CN切片.csv、cn/cn-slice-report.json
```

`tools/packing/` 的两个生成器在展开目录不存在时会自动把本目录的 zip 解到临时目录，所以仅做建包/校验时无需手工解压；跑 `legal_corpus_tools.preprocess` 子命令前则必须先解压。

## 2. 内容与摘要

| 压缩包 | 条目 | 大小 | sha256 | 内容 |
|---|---|---|---|---|
| `hk-sliced-20261105.zip` | 1,024 | 26.69 MiB | `1e0831568f5be158c5c2c9ad946e89ac7c082b23ca0c1b0276ce5798708f34ee` | 64 部法例 / 128 个语言版本（XML → 条文 → 分片） |
| `sg-sliced-20261105.zip` | 702 | 14.43 MiB | `75ee87caca3479e3fc8e74bc0fe8f02dd1ac08ae48b1dacd63116c558f6f6a37` | 78 部法例 / 78 个语言版本（PDF → 条文 → 分片） |
| `cn-sliced-20261107.zip` | 513 | 2.40 MiB | `10cf6d7122396fdf372d7830b9298af4077157524ae55c5a46fc4ce41c161794` | 73 部法例 / 73 个语言版本（docx → 条文 → 分片）+ CN 批次清单 |

合计 2,239 个条目。每个语言版本 7 文件：`sources.json`、`provisions.jsonl`、`chunks.jsonl`、`preview.md`、`validation.json`、`checksums.json`、`extraction-report.json`（SG 另有 `raw-pages.jsonl`），外加 `cache-key.json`。

压缩包由 Python `zipfile` 写入（非 ASCII 文件名带 UTF-8 标志位），macOS `unzip` 与 Python `zipfile` 解压结果一致。

- `hk-sliced` / `sg-sliced` 与生成 `03-import-packs/` 时使用的字节一致。
- `cn-sliced` 内的 `cn/cn-slice-report.json` 中 `source_root` / `output` 已改写为相对路径（`01-original/中国`——把 cn 原件包解压到 `01-original/` 后即此路径；`02-sliced/preprocess-first-batch-20261105/cn`），其余成员与原始产物逐字节一致；因此本包 sha256 与建包时记录的来源包不同，但**不影响由它重建出的正式包**（重建结果与已交付的三个包 `package_id` 相同）。

## 3. 用途

- 重建正式导入包：见根目录 `README.md` §7.5。
- 审核正文/分片：解压后看 `chunks.jsonl` 与 `preview.md`。
- **不可变交付物**：不要用重新切片的结果覆盖本目录的 zip；确需变更走根目录 `README.md` §7 全流程。
