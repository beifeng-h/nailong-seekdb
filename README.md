# nailong-seekdb

Database competition, problem 1: deploy and start a SeekDB instance and
submit its telemetry ID.

## Layout

    seekdb/            the deployment itself
      README.md        deployment record, telemetry ID, Windows gotchas
      telemetry.json   the running observer's run/telemetry.json
      vendor/          third-party artifacts (the MSI is fetched, not committed)
      tools/           msi_extract.py, verify_sql.py and their vendored deps

## Telemetry ID of this deployment

    1D90353A85D1CDBDAAF69B28612F001CAC5E13624943702EFF631D2D2E3A4169

It is the `content.id` field of `seekdb/telemetry.json`, which the running
observer wrote to `<base-dir>/run/telemetry.json`.

See `seekdb/README.md` for the full record: how the instance was deployed,
how to start and stop it, how to redeploy from the official MSI, and how to
submit the ID.

## 赛题一览

| 赛题 | 目录 | 说明 |
| --- | --- | --- |
| 赛题一：部署并启动 seekdb 实例，提交遥测 ID | [`seekdb/README.md`](seekdb/README.md) | WSL/Linux 与 Windows 两套部署的完整记录 |
| 赛题二：修复 Issue #1489 | [`seekdb/issues/1489/README.md`](seekdb/issues/1489/README.md) | 向量检索下等价过滤条件导致的延迟差异 |
| 赛题一：全文索引调优 | [`seekdb/fts-tuning/README.md`](seekdb/fts-tuning/README.md) | seekdb FTS 分词路径的内核优化、语义等价性验证与统一基准测试 |
