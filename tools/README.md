# tools：切片与建包工具

两段链路，按「先切片、后建包」分开：`slicing/` 产出 7 文件切片，`packing/` 把切片聚合成正式导入包。全程离线，不联网、不写数据库。

## 1. 依赖

Python 3.11+：

```bash
cd tools/slicing/legal-corpus-tools
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

`requirements.txt` 锁定 6 个包的版本，`requirements.lock.json` 记录 wheel 摘要。仓库不携带 `site-packages` 副本；如需完全离线，把固定版本 wheel 解压到 `legal-corpus-tools/.vendor/`，工具会自动优先加载（存在才加载，不存在就用当前解释器）。

`config/tokenizer/cl100k_base.tiktoken` 是离线编码资源，`config/chunk-profile.local.json` 里登记了它的 sha256，摘要不符会直接报错而不是降级或联网下载。

## 2. 目录

```text
tools/
├── slicing/
│   ├── legal-corpus-tools/
│   │   ├── legal_corpus_tools/preprocess.py          # 统一 CLI：inventory / run / validate / compare / stop
│   │   ├── legal_corpus_tools/preprocessing/         # 抽取（hk_xml / sg_pdf）、分片、校验、报告
│   │   ├── config/                                   # 切片 profile、离线编码资源、示例配置
│   │   └── tests/                                    # 针对真实风险的测试
│   └── slice_cn.py                                   # CN docx → 同契约切片
└── packing/
    ├── build_official_packs.py                       # HK/SG 切片 → 正式包
    ├── build_cn_pack.py                              # CN 切片 → 正式包（复用上面同一套聚合与校验）
    └── package_validator.py                          # 7 文件契约校验（纯内存）
```

## 3. HK / SG 链路

原件以 zip 形式交付，先解压到 `01-original/`（`cd 01-original && for f in *.zip; do unzip -q -o "$f"; done`），`SRC` 才指向得到目录树。

```bash
cd tools/slicing/legal-corpus-tools
SRC=../../../01-original
OUT=../../../02-sliced/preprocess-first-batch-20261105
PROFILE=config/chunk-profile.local.json

python3.11 -m legal_corpus_tools.preprocess inventory --source-root "$SRC" --output "$OUT"
python3.11 -m legal_corpus_tools.preprocess run  --source-root "$SRC" --output "$OUT" --profile "$PROFILE" --resume
python3.11 -m legal_corpus_tools.preprocess validate --output "$OUT"
```

- `inventory`：读 `01-original/索引_原件.csv`，核验每个原件的字节数与 sha256，按法域/法名/语言选源，输出 `inventory.json`。
- `run`：抽取 → 条文 → 分片，每个语言版本写 7 文件 + `cache-key.json`。`--resume` 按 cache-key（原件摘要 + 解析器/分片器版本 + profile 摘要）跳过未变化的版本；`--law-id` 只跑指定法律。
- `validate`：跨版本一致性检查，输出 `validation.json` 与批次汇总。
- `compare`：可选，渲染官方原件页面与抽取文本对照（对照清单在 `compare.py` 顶部的固定法例列表）。

## 4. CN 链路

```bash
python3.11 tools/slicing/slice_cn.py            # 全量
python3.11 tools/slicing/slice_cn.py --limit 3  # 代表性试跑
```

CN 是 docx，走独立脚本而非 `preprocess` CLI（后者对法域/格式有硬编码，改造会递增 `PARSER_VERSION` 并使 HK/SG 已有产物的 cache-key 失效）。`slice_cn.py` 复用同一套 `chunker` / `tokenizer` / `common`，产出同契约的 7 文件，并写 `cn/索引_CN切片.csv` 与 `cn/cn-slice-report.json`。

## 5. 建包

```bash
python3.11 tools/packing/build_official_packs.py   # HK / SG
python3.11 tools/packing/build_cn_pack.py          # CN
```

- 输入 `02-sliced/`：优先读展开目录，不存在则把该目录的 zip 解到临时目录。
- 输出 `03-import-packs/`：`<jurisdiction>-official-pack-<日期>.zip` 与一份 `official-packs-*.json` 报告（只写相对路径）。
- 落盘前后各跑一次 `validate_archive` + `validate_pack`，`package_id` 由包内容摘要决定，内容变化即变化。

## 6. 测试

```bash
cd tools/slicing/legal-corpus-tools
.venv/bin/python -m pytest tests -q
```

测试默认读仓库内 `01-original/`（可用环境变量 `LEGAL_CORPUS_ROOT` 指向别的语料根）；语料目录不存在时相关用例自动跳过，纯逻辑用例照常运行。
