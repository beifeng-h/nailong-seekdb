# SeekDB 部署记录

赛题一：部署并启动一个 seekdb 实例，提交该实例的遥测 ID。

## 提交到提测平台的部署 UUID

```
1c461b9d-973e-8c7e-9a3c-29099836cf43
```

来自 Linux（WSL）部署的 `run/telemetry.json` 的 `content.id`。这是 **UUID 格式**，
和提测平台「部署 UUID」字段、以及题面"保留完整内容及其中的连字符"的要求一致。

随时可重新读取：

```powershell
wsl -d seekdb-ubuntu -u root -- bash wsl\get_telemetry_id.sh
```

## 怎么提交

**不用提交任何文件。** 提测平台赛题一的提交入口要的是一个字符串：部署 UUID。

1. 登录大赛提测平台
2. 进入 **赛题一** 的提交入口
3. 在「部署 UUID」字段填入上面那串
4. 提交，并核对填进去的内容与 `run/telemetry.json` 的 `content.id` 一致

    1c461b9d-973e-8c7e-9a3c-29099836cf43

「报名仓库地址」填本仓库：`https://github.com/beifeng-h/nailong-seekdb.git`。
「本队验证行」点按钮自动生成；「Fork 仓库地址」「官方 PR 链接」没做就留空。

不要提交：整个 `telemetry.json` 文件、文件路径、AMD 账号、算力券码。

## 两套部署

跑了两套实例，因为**遥测 ID 的格式在版本之间变过**：

| | Linux / WSL 部署 | Windows 部署 |
| --- | --- | --- |
| 版本 | **v1.4.0.0** | v1.3.0.0 |
| `content.id` | **UUID**，带连字符 | 64 位十六进制，无连字符 |
| `telemetryVersion` | 5 | 1 |
| ID 生成方式 | HMAC(machine-id + base-dir) | SHA256(ip + port + **时间戳**) |
| 重启后 ID | **不变** | 重新 bootstrap 会变 |
| 端口 | 2882 | 2881 |
| 详情 | [`wsl/README.md`](wsl/README.md) | 本文下方 |

题面给的示例是 UUID，对应的是 v1.4.0 及以后（也就是 AMD 平台那一版）。v1.3.0 是
**唯一发布过 Windows 构件**的版本，它产出的是纯 hex。Linux 那套用 WSL 跑 v1.4.0，
拿到的就是题面描述的 UUID。**提交用 Linux 这套。**

## Linux（WSL）部署概览

| 项目 | 值 |
| --- | --- |
| 部署平台 | WSL2，Ubuntu 22.04.5 LTS，glibc 2.35 |
| SeekDB 版本 | 1.4.0.0（source revision `1-0003f8d4b32a2cfe3b75ae8f4dbe26fcee994541`） |
| 安装包 | 官方 release v1.4.0 的 `ubuntu22.04_amd64.deb` |
| WSL 发行版 | `seekdb-ubuntu`，位于 `D:\wsl\seekdb-ubuntu` |
| 二进制 | `/usr/bin/seekdb` |
| 配置 | `/etc/seekdb/seekdb.cnf` |
| 运行目录 | `/var/lib/seekdb` |
| 监听端口 | 2882（2881 被同机的 Windows 实例占用了） |
| 登录方式 | `root`，空密码 |

Windows 侧可以直接连（WSL 的 localhost 转发）：

```powershell
python tools\verify_sql.py 2882
```

## Windows 部署概览

| 项目 | 值 |
| --- | --- |
| 部署平台 | 本地 Windows 开发环境（Windows 11 x86_64，Build 26200） |
| SeekDB 版本 | 1.3.0.0（source revision `100000092026051510-a76430272ff328f3ae9085bd48c8622656b3cb19`） |
| 安装包 | `vendor/seekdb-1.3.0.0-win64.msi`（官方 release v1.3.0，全部版本中唯一的 Windows 构件） |
| 运行目录 | `%TEMP%\seekdb` |
| 监听端口 | 2881（MySQL 协议） |
| 进程 PID | 见 `%TEMP%\seekdb\base\observer.pid` |
| 登录方式 | `root`，空密码 |

目录结构：

```
%TEMP%\seekdb\
  app\bin\seekdb.exe     服务端可执行文件
  app\etc\seekdb.cnf     配置
  app\share\              内置 SQL / help / timezone 资源
  base\store\             数据
  base\log\seekdb.log     服务端日志
  base\run\telemetry.json 遥测文件
```

这台实例的 ID 留在 `telemetry.windows-v1.3.0.json` 里备查：

```
1D90353A85D1CDBDAAF69B28612F001CAC5E13624943702EFF631D2D2E3A4169
```

## 项目位置（已接入 D 盘）

项目本体在 **D 盘**，C 盘工作区通过 NTFS 目录联接（junction）指向它，
因此两边的路径读写的是同一份文件：

```text
D:\数据库大赛\                       <- 项目本体（权威副本）
  .git\
  seekdb\                            <- 本 README 及全部脚本

C:\Users\FOR FUTURE\Documents\ChatGPT\数据库大赛\
  .git\                              <- Codex 沙箱保护，无法改为联接
  seekdb\  --junction-->  D:\数据库大赛\seekdb
```

要点：

* `D:\数据库大赛` 是权威副本；C 盘只保留一个联接入口，不再有第二份实体文件。
* `D:\数据库大赛\.git` 与工作区的 `.git` 各自独立：工作区的 `.git` 被 Codex
  沙箱设为只读，无法替换为联接。**提交历史以 `D:\数据库大赛` 侧的仓库为准**，
  需要历史记录时请在 D 盘路径下执行 git 命令。
* 实例的运行时目录与项目是否放在 D 盘无关——`D:\数据库大赛` 同样含中文，
  路径非 ASCII 的限制依旧成立（见文末"两个坑"）。

## 常用命令

```powershell
# 读取要提交的 UUID（Linux 部署）
wsl -d seekdb-ubuntu -u root -- bash wsl\get_telemetry_id.sh

# 启动 / 停止 Linux 部署
wsl -d seekdb-ubuntu -u root -- bash wsl\start_seekdb.sh
wsl -d seekdb-ubuntu -u root -- bash wsl\stop_seekdb.sh

# 启动 / 停止 Windows 部署
.\start_seekdb.ps1
.\stop_seekdb.ps1

# 验证 SQL
python tools\verify_sql.py 2882   # Linux v1.4.0
python tools\verify_sql.py 2881   # Windows v1.3.0
```

## 两个 Windows 上的坑

**1. MSI 无法直接安装。** `msiexec /a` 和 `msiexec /i` 都会在最后的
custom action 阶段失败（错误 2503/1603），该 action 需要管理员权限注册
Windows 服务。因此改为直接抽取 MSI 内的载荷：`tools/msi_extract.py` 用
olefile 取出内嵌 cabinet（MSI 会混淆 stream 名，所以按 `MSCF` magic 识别），
用 `expand.exe` 解包，再依据 MSI 自身的 Component/Directory/File 表还原目录
结构。全程不需要管理员权限。

**2. 路径必须全 ASCII。** SeekDB 把路径交给 SQLite 时按本地 ANSI 代码页
转换，而 SQLite 要求 UTF-8，路径中含非 ASCII 字符时直接报
`unable to open database file`。本项目目录名 `数据库大赛` 含中文，因此
Windows 实例的运行时目录放在 `%TEMP%\seekdb`（纯 ASCII）。8.3 短路径同样
无效——中文目录的短名依然不是 ASCII。

## 关于遥测 ID 的生成

见 `src/share/ob_telemetry.cpp`：

* 文件名常量 `TELEMETRY_FILE_NAME = "run/telemetry.json"`，相对运行目录。
* **v1.3.0**：`content.id` = `SHA256(ip + port + 时间戳)` 的十六进制，64 位、
  无连字符，且**每次重新 bootstrap 都会变**。
* **v1.4.0**：`content.id` = `generate_telemetry_uuid()`，由
  `HMAC-SHA256(app_id + base_dir + scope_id + machine_id)` 派生，输出标准
  **UUID**（带连字符），**与时间戳无关，重启不变**。
* 开关是环境变量 `TELEMETRY_ENABLED`，只有显式设为 `false` 才关闭，默认开启。
* `report_telemetry()` 在实例 bootstrap 时调用一次，所以文件在启动时生成。
* 同一份内容还会尝试上报到 `https://openwebapi.oceanbase.com/api/web/oceanbase/report`。
  本机网络受限，上报失败只在日志里记一条 WDIAG，不影响本地文件和 ID 生成。

## GitHub 仓库

项目推送到 <https://github.com/beifeng-h/nailong-seekdb.git>（仓库本体在
`D:\数据库大赛`，C 盘工作区通过目录联接指向它）。推送步骤见
`push_to_github.md`。

* `seekdb/vendor/*.msi` 不入库：安装包 150 MB，超过 GitHub 单文件 100 MB 硬限制。
  下载地址与 SHA-256 见 `seekdb/vendor/README.md`。
* `__pycache__` 等由 `.gitignore` 排除。

---
