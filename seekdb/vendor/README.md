# Vendor artifacts

`seekdb-1.3.0.0-win64.msi` (158,019,584 bytes) is **not** committed here:
GitHub refuses git objects larger than 100 MB.

Fetch it with:

    curl -L -o seekdb-1.3.0.0-win64.msi \
      https://github.com/oceanbase/seekdb/releases/download/v1.3.0/seekdb-1.3.0.0-100000092026051510-win64.msi

| | |
| --- | --- |
| SHA-256 | `9aeef80482c4460ddfc2c54948f60b2e6280869b9d40ecc2748055072f0e2241` |
| Size | 158,019,584 bytes |
| Source | [oceanbase/seekdb v1.3.0](https://github.com/oceanbase/seekdb/releases/tag/v1.3.0) |

It is the only Windows artifact ever published for SeekDB -- every other
release asset targets Linux or macOS.

`..\deploy_seekdb.ps1` extracts the payload out of this installer without
needing administrator rights.
