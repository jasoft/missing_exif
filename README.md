# missing_exif

扫描目录中的图片和视频文件（包含 HEIF/HEIC），当文件缺失拍摄时间元数据时，将文件系统“最后修改时间”写入元数据。

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
  -v /volume1/photo_backup:/backup \
  missing-exif:latest \
  /data --dry-run --backup-dir /backup --progress-interval 20
```

### 3. 正式执行（写入元数据）

```bash
docker run --rm -it \
  -e TZ=Asia/Shanghai \
  -v /volume1/photo:/data \
  -v /volume1/photo_backup:/backup \
  missing-exif:latest \
  /data --backup-dir /backup --progress-interval 20
```

如果你不想交互确认，可加 `-y`：

```bash
docker run --rm -it \
  -e TZ=Asia/Shanghai \
  -v /volume1/photo:/data \
  -v /volume1/photo_backup:/backup \
  missing-exif:latest \
  /data --backup-dir /backup -y --progress-interval 20
```

## 参数说明

- `target_dir`：要扫描的目录（容器内路径，例如 `/data`）
- `--dry-run`：只显示将被修改的文件，不执行写入
- `--backup-dir`：备份目录，建议映射到独立卷（例如 `/backup`）
- `--progress-interval`：扫描阶段进度输出间隔（默认每 50 个媒体文件输出一次）
- `-y` / `--yes`：跳过交互确认，直接执行写入
