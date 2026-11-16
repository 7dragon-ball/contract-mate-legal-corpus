# 01-original：原始法律文本

导入时采用的同一批原件，按法域打包成 zip；索引平铺在本目录。**本目录只读**，切片工具按索引定位文件、不扫目录。

未解压时这里只有 3 个压缩包 + 2 个索引；解压后即恢复「法域 / 类目 / 法名 / 语言」目录树（见 §3）。

## 1. 文件清单

| 文件 | 内容 | 体积 |
|---|---|---|
| `hk-original-20261105.zip` | `香港/`，128 个官方 XML（64 部法 × 中英双语） | 9.76 MiB |
| `sg-original-20261105.zip` | `新加坡/`，78 个官方 PDF（78 部法） | 25.65 MiB |
| `cn-original-20261107.zip` | `中国/`，73 个官方 docx + `索引_CN原件.csv` | 1.84 MiB |
| `索引_原件.csv` | HK/SG 共 206 件的索引 | 62 KB |
| `索引_CN原件.csv` | CN 共 73 件的索引，与 cn 包内那份内容相同 | 17 KB |

## 2. 压缩包

三个包与三个法域目录一一对应，解压后与打包时的目录逐文件字节一致（已逐条 sha256 比对）。

| 文件 | 内含 | 条目 | 原始 | zip | sha256 |
|---|---|---|---|---|---|
| `hk-original-20261105.zip` | `香港/` | 128 | 63.69 MiB | 9.76 MiB | `daa6b410f24c6cfe8aa1c7844ba5afd3319dde2a06b4e306dad26b99f198825b` |
| `sg-original-20261105.zip` | `新加坡/` | 78 | 27.34 MiB | 25.65 MiB | `564f6febcd6fa48af6f44275391d601dbfdee75686972f5c1b7ee0f49cd07e42` |
| `cn-original-20261107.zip` | `中国/` 的 73 个 docx + `索引_CN原件.csv` | 74 | 1.91 MiB | 1.84 MiB | `ad0a3125280585854cabe6fad1cd885bb9620872f3ddb6d5529b3496141e4111` |

- zip 内顶层就是中文目录名（`香港/`、`新加坡/`、`中国/`），与 `索引_原件.csv` 中 `相对路径` 的前缀一致。
- `cn-original-*.zip` 含 `索引_CN原件.csv`，不含本 README——文档改动不影响该包摘要。
- 打包用 Python `zipfile`（非 ASCII 文件名写入 UTF-8 标志位）。不要用系统 `zip` 重打，中文名可能按本地编码写入，解压乱码。
- macOS 自带 Info-ZIP 6.00 在 `LANG=C` 的终端里 `unzip -l` 会把中文名显示成 `?`，**只是列表显示问题**，解压出的文件名正常（`unzip`、`ditto -x -k`、双击解压均已验证）；要看列表可用 `python3.11 -m zipfile -l <zip>`。

## 3. 解压

在本目录解压即恢复原布局，工具与索引都按这个布局取数：

```bash
cd 01-original && unzip -q hk-original-20261105.zip && unzip -q sg-original-20261105.zip
# 只跑某一个法域时，解压对应的那个包即可
```

以下命令在仓库根（`legal-corpus-public/`）执行：

```bash
# 按索引逐件校验解压结果（HK/SG，206 件）
python3.11 - <<'PY'
import csv, hashlib, pathlib
root = pathlib.Path('01-original')
rows = list(csv.DictReader(open('01-original/索引_原件.csv', encoding='utf-8')))
bad = [r['相对路径'] for r in rows
       if hashlib.sha256((root / r['相对路径']).read_bytes()).hexdigest() != r['sha256']]
print(len(rows), '件校验完成，不符', len(bad), bad[:3])
PY
```

解压出的文件不属于交付清单：需要还原成「只有 zip」的状态时，删掉解压目录即可（`rm -rf 香港 新加坡 中国`），交付摘要以 `MANIFEST.sha256` 为准。

## 4. 索引

| 文件 | 覆盖 | 用途 |
|---|---|---|
| `索引_原件.csv` | HK/SG 共 206 件 | 切片工具的输入：`相对路径` / `字节数` / `sha256` 与实际文件逐件校验，不符则该方法律判 `blocked`；另含 `官方链接` 与 `来源批次` |
| `索引_CN原件.csv` | CN 共 73 件 | 审计用，不参与切片；同时记录交付副本与切片时原件的摘要 |

`相对路径` 相对本目录（即解压后 `01-original/`），不含任何本机路径；未解压时无法就地校验，文件只存在于 zip 内。`来源批次` 是该件的语料来源批次名（官方全集 / SSO 补充下载）。

## 5. CN 原件的元数据清理

73 个 docx 的 `docProps/core.xml` 中，`dc:creator` 与 `cp:lastModifiedBy` 已删除（原值含机器名与个人姓名）；其余部件未改动，`word/document.xml` 与切片时使用的原件**逐字节一致**。

因此：

- `索引_CN原件.csv` 的 `sha256` / `字节数` 指**本目录交付的副本**（即 zip 内那份）；
- `切片时_sha256` / `切片时_字节数` 指生成 `02-sliced/cn-sliced-*.zip` 时所用原件；
- 两者只差 `docProps/core.xml` 这一个部件，正文完全一致。

HK 的 XML 与 SG 的 PDF 不含个人元数据（PDF 的 Author/Producer 为官方排版系统），未做任何改动。

## 6. 未包含的内容

- 早期收集的 HTML 快照、图片、未采用的候选件；
- 来源采集阶段的清单与调研记录。

理由：这些材料与最终切片无关，且包含采集过程中的内部判断记录。选源结论已体现在本目录的文件集合中。
