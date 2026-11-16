# 香港 / 新加坡 / 中国内地 法律语料（贸易合同场景）

离线整理的法律语料交付包：原件、切片产物、可直接导入的正式包、以及可复现的切片与建包工具。

- 覆盖 **215 部**法律：香港 64 部（中英双语，128 个语言版本）、新加坡 78 部、中国内地 73 部。
- 切片 **82,935 个 chunk** / **81,950 条 provision**，7 文件标准包结构，纯离线生成，全过程不联网。
- 本目录不含任何运行日志、核验截图或内部调研材料；元数据中不记录本机路径。

## 1. 目录结构

```text
legal-corpus-public/
├── README.md
├── MANIFEST.sha256            # 全量文件摘要清单
├── 01-original/               # 原始法律文本（3 个按法域打包的 zip + 2 份索引）
├── 02-sliced/                 # 切片产物（压缩包，按法域分包）
├── 03-import-packs/           # 正式导入包（7 文件标准包，可直接导入）
└── tools/
    ├── slicing/               # 原件 → 切片（HK/SG 走 preprocess CLI，CN 走 slice_cn.py）
    └── packing/               # 切片 → 正式导入包（含契约校验器）
```

## 2. 导入用哪些文件

只有三个 zip，一个法域一个包，互不依赖，可只导其中一个。

| 法域 | 文件 | 大小 | laws / editions | provisions / chunks | package_id |
|---|---|---|---|---|---|
| HK | `03-import-packs/hk-official-pack-20261105.zip` | 25.72 MiB | 64 / 128 | 68,918 / 69,525 | `bb36b771c3d9f8f57b3a00da9fe58cdd0194136b3cad766a8aba3e711e584a37` |
| SG | `03-import-packs/sg-official-pack-20261105.zip` | 5.83 MiB | 78 / 78 | 7,089 / 7,467 | `eb5224b45aa637b5592a7421cdd524875d1e166671f8cf6712c8444d65ed5d62` |
| CN | `03-import-packs/cn-official-pack-20261107.zip` | 2.03 MiB | 73 / 73 | 5,943 / 5,943 | `bacf4652e27f0f6111a0279c40d6f620844da84085f9fd6305037137a5e44840` |

每个包都是 7 个文件：`manifest.json`、`catalog-snapshot.csv`、`chunks.jsonl`、`provisions.jsonl`、`sources.json`、`validation.json`、`checksums.json`。

导入前确认「拿到的就是这一版」，任选一种：

```bash
shasum -a 256 03-import-packs/*.zip          # 与 MANIFEST.sha256 一致
unzip -p 03-import-packs/hk-official-pack-20261105.zip manifest.json | head -20   # 看 package_id
```

三条不要做：

- **不要解包改内容再打包**：`package_id` 与 sha256 都会变，就不再是这一版。
- **不要用 `02-sliced/` 重新打包的结果替换这三个 zip**：内容等价但 zip 字节不同，sha256 对不上。
- **不要把 `03-import-packs/*.json` 当导入输入**：它们只是建包报告。

导入动作本身在接收方完成（写库、切换法域状态），需要授权与维护窗口；本目录全程只读。

## 3. 校验完整性

```bash
shasum -a 256 -c MANIFEST.sha256 | grep -v ': OK$'    # 应无输出
```

## 4. 原件

见 `01-original/README.md`。原件按法域打包：`hk-original-20261105.zip`（香港，128 个 XML）、`sg-original-20261105.zip`（新加坡，78 个 PDF）、`cn-original-20261107.zip`（中国内地，73 个 docx）。索引平铺在 `01-original/`：`索引_原件.csv`（HK/SG，206 件）与 `索引_CN原件.csv`（CN，73 件），记录每部法的相对路径、官方链接与摘要。

本目录只存压缩包，切片前先解压——解压即恢复工具与索引期望的目录树：

```bash
cd 01-original && for f in *.zip; do unzip -q -o "$f"; done
```

## 5. 切片产物

见 `02-sliced/README.md`。解压到同一批次目录即可复原「法域 / 法名 / 语言 / 版本」目录树与 7 文件。

## 6. 工具

见 `tools/README.md`。依赖 Python 3.11。两段链路都可以从本目录的原始文件从头跑出来。

## 7. 新增法律文本的流程

**正式包是全量快照**，不是增量补丁：新增一部法律 = 该法域的包整体重建、`package_id` 变、需要重新导入该法域；另外两个法域不受影响。

### 7.1 放原件

原件在 `01-original/*.zip` 里，先解开对应的包（`cd 01-original && unzip -q -o hk-original-20261105.zip`），再按下述布局放文件：

```text
01-original/香港/<主法|附属法例|商品行业扩展>/Cap.<号>_<中文名>/全集_中文/cap_<号>_<时间戳>_zh-Hant_c.xml
01-original/香港/<...>/Cap.<号>_<中文名>/全集_英文/cap_<号>_<时间戳>_en_c.xml
01-original/新加坡/<类别>/<CODE>_<中文名>/全集_主法PDF/<官方文件名>.pdf
01-original/中国/<法名>_<YYYYMMDD>.docx
```

- HK 必须有官方完整 XML（HTML 快照不能替代正式原件）。
- SG 必须有官方 PDF；同一语言出现多份不同摘要的 PDF 会被判冲突，需人工确认版本，工具不会自动选最新。
- CN 文件名必须是 `<法名>_<YYYYMMDD>.docx`，`slice_cn.py` 靠这个格式取法名与版本日期。

### 7.2 更新索引

HK/SG 在 `01-original/索引_原件.csv` 增行：`法域` 填 `香港` 或 `新加坡`；`条目类型` 填 `主法` / `附属法例` / `商品行业扩展`（其他类型不进切片）；`相对路径`（相对 `01-original/`）、`字节数`、`sha256` **必须与真实文件一致**——不一致该法律直接判 `blocked`。`清单标识` 决定 `law_id`（HK 形如 `hk:cap26`，SG 形如 `sg:XXX`），定下后不要改。

```bash
shasum -a 256 "01-original/香港/主法/Cap.XXX_中文名/全集_英文/xxxx.xml"
```

CN 在 `01-original/索引_CN原件.csv` 增行（文件名 / 法名 / 版本日期 / 字节数 / sha256），供审计，不参与切片。

改完原件后重打该法域的 zip（用 Python `zipfile`，别用系统 `zip`，见 `01-original/README.md` §2），再删掉解压目录，交付形态才是「只有 zip」。

### 7.3 跑切片

HK / SG（原件先按 §7.1 解压，`SRC` 指解压后的 `01-original/`）：

```bash
cd tools/slicing/legal-corpus-tools
SRC=../../../01-original
OUT=../../../02-sliced/preprocess-first-batch-20261105
PROFILE=config/chunk-profile.local.json

python3.11 -m legal_corpus_tools.preprocess inventory --source-root "$SRC" --output "$OUT"
# 先单跑新增法例，确认无误再全量收口
python3.11 -m legal_corpus_tools.preprocess run --source-root "$SRC" --output "$OUT" \
  --profile "$PROFILE" --resume --law-id hk:capXXX
python3.11 -m legal_corpus_tools.preprocess run --source-root "$SRC" --output "$OUT" \
  --profile "$PROFILE" --resume
python3.11 -m legal_corpus_tools.preprocess validate --output "$OUT"
```

`--resume` 按 cache-key（原件摘要 + 解析器/分片器版本 + profile 摘要）跳过未变化的版本，所以新增一部只重算这一部。选源失败的法律生成 `...:blocked` 目录，不中断整批。

CN：

```bash
python3.11 tools/slicing/slice_cn.py          # 自动纳入新增 docx，重写 cn/ 下全部版本
```

看 `cn/索引_CN切片.csv` 与 `cn-slice-report.json` 的 `fallback_laws`（无「第X条」结构、按 6 段兜底分组的法律）。

### 7.4 重打切片压缩包

必须用 Python `zipfile`：macOS 自带 `zip`（Info-ZIP）不给非 ASCII 文件名写 UTF-8 标志位，Python 解压会得到乱码路径。

```bash
cd 02-sliced
python3.11 - <<'PY'
import pathlib, zipfile
STAMP = '20261105'
BATCH = pathlib.Path('preprocess-first-batch-20261105')
DOMAINS = ['hk', 'sg', 'cn']

def entries(root):
    p = BATCH / root
    items = [p] if p.is_file() else sorted(p.rglob('*'))
    return [i for i in items if i.is_file() and i.name != '.DS_Store']

for d in DOMAINS:
    name = f'{d}-sliced-{STAMP}.zip'
    with zipfile.ZipFile(name, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in entries(d):
            zf.write(f, f.relative_to(BATCH).as_posix())
    print(name, pathlib.Path(name).stat().st_size)
PY
```

条目路径相对批次根（`hk/...`、`sg/...`、`cn/...`）。

### 7.5 重建正式包并校验

```bash
python3.11 tools/packing/build_official_packs.py   # HK / SG
python3.11 tools/packing/build_cn_pack.py          # CN
```

两个生成器都只读 `02-sliced/`（展开目录不存在时自动解压 zip 到临时目录），写 `03-import-packs/`，落盘前跑 `validate_archive` + `validate_pack` 双重校验。内容变了会得到**新的 `package_id`**，属正常。

### 7.6 更新记录

1. 更新本文 §2 表的 `package_id`、大小与计数。
2. 重建清单：

```bash
cd legal-corpus-public && rm -f MANIFEST.sha256
find . -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/manifest.new && mv /tmp/manifest.new MANIFEST.sha256
shasum -a 256 -c MANIFEST.sha256 2>&1 | grep -v ': OK$'    # 应无输出
```

清单写临时文件再移动，避免把清单自身（空文件）记进清单。

## 8. 已知边界

- **CN 表格丢失行列关系**：CN 抽取按段落读 `word/document.xml`，表格内容被压成普通段落；HK/SG 分别保留了 `table`、`schedule` 元素。
- **CN 条文粒度**：以「第X条」正则切分，无该结构的文件（如刑法修正案）按 6 段兜底分组，`kind` 只有 `section` / `long_title`，与 HK/SG 的 `table` / `note` / `heading` / `schedule` 不同粒度。
- **版本选择**：HK/SG 由索引里的 `sha256` 定版，工具不会联网核对「是否最新有效版本」，`verification_status` 统一为 `pending`；原件的法律时效未经独立核验。
- **语言覆盖**：HK 中英双语；SG 与 CN 仅英文 / 简体中文。
- **切片不可变**：`02-sliced/` 的 zip 与 `03-import-packs/` 的 zip 一经交付即冻结；变更须走 §7 全流程并重新导入。
- **复核材料不在本目录**：批次运行日志、原件对照截图与核验结论属内部审计材料，未随本目录发布；如需复核抽取质量，可按 §7 重跑并在输出目录查看 `report.md`、`validation.json`、`extraction-report.json`。

## 9. 来源与再分发

- 香港：电子版香港法例（eLegislation）；新加坡：Singapore Statutes Online（SSO）；中国内地：官方发布的法律文本。每部法的官方链接与版次见 `01-original/索引_原件.csv` 与各切片版本目录下的 `sources.json`（含 `required_notice`）。
- 三方门户对批量转载各有条款约束（eLegislation Important Notices、SSO Terms of Use），**对外再分发前请自行确认适用条款**；本目录仅做技术整理，不改变原件版权归属。
- CN 原件副本的文档属性（作者 / 最后修改者）已清除，正文未改；`索引_CN原件.csv` 同时记录交付副本与切片时原件的摘要，便于回溯对应关系。
