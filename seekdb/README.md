# SeekDB 本地部署记录

赛题一：部署并启动一个 seekdb 实例，提交该实例的遥测 ID。

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

* `D:\数据库大赛` 是权威副本；C 盘只保留一个联接入口，不再有第二份实体文件
  （原先 C 盘上的 158 MB MSI 副本已删除，C 盘占用相应减少）。
* `D:\数据库大赛\.git` 与工作区的 `.git` 各自独立：工作区的 `.git` 被 Codex
  沙箱设为只读，无法替换为联接。**提交历史以 `D:\数据库大赛` 侧的仓库为准**，
  需要历史记录时请在 D 盘路径下执行 git 命令。
* 实例的运行时目录仍然是 `%TEMP%\seekdb`，与项目是否放在 D 盘无关——
  `D:\数据库大赛` 同样含中文，路径非 ASCII 的限制依旧成立（见文末“两个坑”）。

## 本次部署的遥测 ID

```
1D90353A85D1CDBDAAF69B28612F001CAC5E13624943702EFF631D2D2E3A4169
```

来源：运行目录下 `run/telemetry.json` 的 `content.id` 字段。

随时可重新读取：

```powershell
.\get_telemetry_id.ps1
```

## 怎么提交

**不用提交任何文件。** 大赛提测平台赛题一的提交入口要的是一个字符串：遥测 ID。

1. 登录大赛提测平台
2. 进入 **赛题一** 的提交入口
3. 把下面这串填进对应的遥测 ID 字段
4. 提交，并核对填进去的内容与 `run/telemetry.json` 的 `content.id` 一致

    1D90353A85D1CDBDAAF69B28612F001CAC5E13624943702EFF631D2D2E3A4169

不要提交：整个 `telemetry.json` 文件、文件路径、AMD 账号、算力券码。

提交前建议自己再读一遍，确认拿到的是当前实例的值：

    .\get_telemetry_id.ps1

两点注意：

* 实例重启（重新 bootstrap）会生成新的 ID，**提交前不要重建实例**。普通重启
  不重写 `telemetry.json`，ID 保持不变。
* 题目建议保存一张部署成功截图备查（不必上传）：跑 `get_telemetry_id.ps1` 的
  终端窗口截图即可。

---

## 部署概况

| 项目 | 值 |
| --- | --- |
| 部署平台 | 本地 Windows 开发环境（Windows 11 x86_64，Build 26200） |
| SeekDB 版本 | 1.3.0.0（`OceanBase seekdb 1.3.0.0`，source revision `100000092026051510-a76430272ff328f3ae9085bd48c8622656b3cb19`） |
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
  base\run\telemetry.json 遥测文件  <- 遥测 ID 在这里
```

## 验证结果

```
VERSION()      -> 5.7.25-OceanBase seekdb-v1.3.0.0
DATABASES      -> information_schema, mysql, oceanbase, sys_external_tbs, test
CREATE/INSERT/SELECT table t -> ((1, 'hello'), (2, 'seekdb'))
```

## 常用命令

```powershell
# 启动（后台，脱离当前终端）
python .\launch_detached.py

# 启动（前台，便于观察日志）
.\start_seekdb.ps1 -Foreground

# 停止
.\stop_seekdb.ps1

# 读取遥测 ID
.\get_telemetry_id.ps1

# 从 MSI 重新部署（机器重启或 %TEMP% 被清理后执行）
# 需要 Python 3.9-3.12（msilib 在 3.13 被移除）；python 不在 PATH 上时用 -Python 指定
.\deploy_seekdb.ps1 -Python "C:\path\to\python.exe"
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
运行时目录放在 `%TEMP%\seekdb`（纯 ASCII）。8.3 短路径同样无效——中文
目录的短名依然不是 ASCII。

## GitHub 仓库

项目推送到 <https://github.com/beifeng-h/nailong-seekdb.git>（仓库本体在
`D:\数据库大赛`，C 盘工作区通过目录联接指向它）。

* `seekdb/vendor/*.msi` 不入库：安装包 150 MB，超过 GitHub 单文件 100 MB 硬限制。
  下载地址与 SHA-256 见 `seekdb/vendor/README.md`。
* `__pycache__` 等由 `.gitignore` 排除。

---

## 关于遥测 ID 的生成

见 `src/share/ob_telemetry.cpp`（v1.3.0）：

* 文件名常量 `TELEMETRY_FILE_NAME = "run/telemetry.json"`，相对运行目录。
* `content.id` 由 `ip + port + 时间戳` 做 SHA-256 后取十六进制，因此是
  **64 位大写十六进制串**，而不是带连字符的 UUID（题目里的 UUID 只是格式
  示意）。
* 开关是环境变量 `TELEMETRY_ENABLED`，只有显式设为 `false` 才关闭，默认开启。
* `report_telemetry()` 只在实例 bootstrap 时调用一次（日志中确认仅 1 次），
  所以同一个实例的 ID 在其生命周期内保持不变；重启实例会生成新的 ID。
* 同一份内容还会尝试上报到 `https://openwebapi.oceanbase.com/api/web/oceanbase/report`。
  本机网络受限，上报失败只在日志里记一条 WDIAG，不影响本地文件和 ID 生成。
