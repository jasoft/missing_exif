# missing_exif

扫描目录中的图片和视频文件（包含 HEIF/HEIC），当文件缺失拍摄时间元数据时，将文件系统“最后修改时间”写入元数据。

## 功能特性

- 递归扫描并跟随目录软链接。
- 支持目录排除（如 `#recycle`、`.thumb`、`@eaDir`）。
- 三阶段流水线：`预扫描 -> 筛选 -> 写回`。
- 每个阶段都可输出/复用 JSONL 中间产物，避免重复扫描。
- 写回前输出待修改清单，支持 `--dry-run`。
- 写入前自动备份原文件，失败时自动回滚。
- 修复 Windows 多线程场景下 `gbk` 解码崩溃（统一按字节读取并安全解码）。

脚本入口：`fill_missing_exif.py`

## 一键运行（默认）

以下命令等价于执行完整流水线：

```bash
python fill_missing_exif.py P:\ --dry-run --backup-dir P:\photo_backup --progress-interval 50
```

说明：

- 默认子命令是 `run`，所以老用法仍可用。
- 中间文件自动保存到备份目录下：`<backup_dir>/.missing_exif_state/`。
- 再次运行时会优先复用中间文件：
  - `discover_<hash>.jsonl`
  - `plan_<hash>.jsonl`
- 如需强制重跑阶段：
  - `--refresh-discover`
  - `--refresh-filter`

## 三阶段单独执行

### 1) 预扫描阶段（discover）

扫描所有符合条件的媒体文件并输出 JSONL：

```bash
python fill_missing_exif.py discover P:\ --backup-dir P:\photo_backup \
  --exclude-dir "#recycle,.thumb,@eaDir,photo_backup" \
  --output P:\photo_backup\.missing_exif_state\discover.jsonl
```

### 2) 筛选阶段（filter）

并发检查哪些文件缺失 EXIF/XMP/QuickTime/Keys/IPTC 元数据：

```bash
python fill_missing_exif.py filter P:\ --backup-dir P:\photo_backup \
  --input P:\photo_backup\.missing_exif_state\discover.jsonl \
  --output P:\photo_backup\.missing_exif_state\plan.jsonl \
  --scan-workers 32 --progress-interval 50
```

### 3) 写回阶段（write）

读取计划并执行写回：

```bash
python fill_missing_exif.py write P:\ \
  --input P:\photo_backup\.missing_exif_state\plan.jsonl \
  --dry-run
```

正式写入：

```bash
python fill_missing_exif.py write P:\ \
  --input P:\photo_backup\.missing_exif_state\plan.jsonl \
  -y
```

## Pipe 串联用法

可以直接通过标准输入/输出串联三个阶段：

```bash
python fill_missing_exif.py discover P:\ --backup-dir P:\photo_backup --output - \
  | python fill_missing_exif.py filter P:\ --backup-dir P:\photo_backup --input - --output - \
  | python fill_missing_exif.py write P:\ --input - --dry-run
```

说明：日志输出在 `stderr`，JSONL 数据在 `stdout`，因此可安全 pipe。

## Docker 运行（群晖可用）

项目提供 `Dockerfile`，容器内包含 `python + exiftool`。镜像主要提供运行环境。

### 1. 构建镜像（仅首次或依赖变更时）

```bash
docker build -t missing-exif:latest .
```

后续仅修改脚本代码时，不需要重建镜像。挂载项目目录到 `/workspace` 后，容器会优先执行挂载目录中的 `fill_missing_exif.py`。

### 2. Dry Run

```bash
docker run --rm -it \
  -e TZ=Asia/Shanghai \
  -v /volume1/docker/missing_exif:/workspace:ro \
  -v /volume1/PhotoHomes:/data \
  -v /volume1/PhotoHomes/photo_backup:/backup \
  missing-exif:latest \
  /data --dry-run --backup-dir /backup --progress-interval 50 \
  --exclude-dir "#recycle,.thumb,@eaDir,photo_backup"
```

### 3. 后台运行（无 TTY）

后台模式不要加 `-it`，并使用 `-y` 跳过交互：

```bash
docker run -d --name missing-exif-job \
  -e TZ=Asia/Shanghai \
  -v /volume1/docker/missing_exif:/workspace:ro \
  -v /volume1/PhotoHomes:/data \
  -v /volume1/PhotoHomes/photo_backup:/backup \
  missing-exif:latest \
  /data --backup-dir /backup --progress-interval 50 -y \
  --exclude-dir "#recycle,.thumb,@eaDir,photo_backup"
```

查看日志：

```bash
docker logs -f missing-exif-job
```

## 常用参数

- `target_dir`：要扫描的目录。
- `--backup-dir`：备份目录（相对路径会拼接到 `target_dir` 下）。
- `--exclude-dir`：按目录名排除，支持重复传入和逗号分隔。
- `--scan-workers`：筛选阶段并发线程数。
- `--progress-interval`：筛选阶段进度输出间隔。
- `--dry-run`：只预览，不执行写回。
- `-y` / `--yes`：跳过写回确认。
- `--refresh-discover`：强制重跑预扫描阶段。
- `--refresh-filter`：强制重跑筛选阶段。
