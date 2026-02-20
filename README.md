# missing_exif

扫描目录中的图片和视频文件（包含 HEIF/HEIC），当文件缺失拍摄时间元数据时，将文件系统“最后修改时间”写入元数据。

特性：
- 递归扫描并跟随目录软链接。
- 扫描阶段实时输出进度（包含“预扫描进度”和“扫描进度”）。
- 自动断点续扫：已扫描且未变化文件会跳过，不需要手动指定 JSON。
- 写入前自动备份原文件，失败自动回滚。

脚本文件：`fill_missing_exif.py`

## Docker 运行（群晖可用）

项目已提供 `Dockerfile`，容器内会安装 `exiftool`，适合主机未安装 exiftool 但可运行 Docker 的场景。

### 1. 构建镜像

```bash
docker build -t missing-exif:latest .
```

### 2. Dry Run（只预览不写入）

```bash
docker run --rm -it \
  -e TZ=Asia/Shanghai \
  -v /volume1/photo:/data \
  -v /volume1:/volume1:ro \
  -v /volume1/photo_backup:/backup \
  missing-exif:latest \
  /data --dry-run --backup-dir /backup --progress-interval 20 \
  --exclude-dir "#recycle" --exclude-dir ".thumb"
```

### 3. 正式执行（写入元数据）

```bash
docker run --rm -it \
  -e TZ=Asia/Shanghai \
  -v /volume1/photo:/data \
  -v /volume1:/volume1 \
  -v /volume1/photo_backup:/backup \
  missing-exif:latest \
  /data --backup-dir /backup --progress-interval 20 -y \
  --exclude-dir "#recycle,.thumb"
```

### 4. 后台运行（长任务推荐）

`nohup` 或后台任务环境没有 TTY，不能使用 `-it`。  
后台运行时请去掉 `-it`，并加 `-y` 避免交互确认阻塞。

使用 `nohup`：

```bash
nohup docker run --rm \
  -e TZ=Asia/Shanghai \
  -v /volume1/photo:/data \
  -v /volume1:/volume1 \
  -v /volume1/photo_backup:/backup \
  missing-exif:latest \
  /data --backup-dir /backup --progress-interval 20 -y \
  --exclude-dir "#recycle,.thumb" \
  > /volume1/photo_backup/missing-exif.log 2>&1 &
```

查看日志：

```bash
tail -f /volume1/photo_backup/missing-exif.log
```

使用 Docker detached 模式（推荐）：

```bash
docker run -d --name missing-exif-job \
  -e TZ=Asia/Shanghai \
  -v /volume1/photo:/data \
  -v /volume1:/volume1 \
  -v /volume1/photo_backup:/backup \
  missing-exif:latest \
  /data --backup-dir /backup --progress-interval 20 -y \
  --exclude-dir "#recycle,.thumb"
```

查看任务日志：

```bash
docker logs -f missing-exif-job
```

## 断点续扫说明

脚本会在备份目录下自动维护隐藏状态文件（`/backup/.missing_exif_state/`），用于断点续扫。  
这些文件对使用者透明，不需要在命令行传任何额外参数。

中断后只需重新执行同一条命令，脚本会自动跳过已扫描且未变化的文件。

## 参数说明

- `target_dir`：要扫描的目录（容器内路径，例如 `/data`）
- `--dry-run`：只显示将被修改的文件，不执行写入
- `--backup-dir`：备份目录，建议映射到独立卷（例如 `/backup`）
- `--progress-interval`：扫描阶段进度输出间隔（默认每 50 个媒体文件输出一次）
- `--exclude-dir`：按目录名排除扫描，支持重复传入和逗号分隔
- `--scan-workers`：扫描阶段并发线程数（默认自动计算）
- `-y` / `--yes`：跳过交互确认，直接执行写入
