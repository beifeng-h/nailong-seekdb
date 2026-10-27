"""Extract the seekdb win64 MSI payload without installing it.

The MSI is an OLE2 container whose single embedded cabinet (media1.cab)
holds every payload file. We pull the cabinet out with olefile, expand it
with expand.exe, then rebuild the directory tree from the MSI's
Component/Directory/File tables.
"""

import os
import subprocess
import sys

import msilib
import olefile

MSI = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])

os.makedirs(OUT, exist_ok=True)


def msi_rows(db, sql, ncols):
    """Read a table, pulling values out while the view is still alive.

    msilib Rows reference the view's current record, so the values must be
    extracted before the view is released.
    """
    view = db.OpenView(sql)
    view.Execute(None)
    out = []
    while True:
        row = view.Fetch()
        if not row:
            break
        vals = []
        for i in range(1, ncols + 1):
            try:
                vals.append(row.GetString(i))
            except Exception:
                vals.append("")
        out.append(tuple(vals))
    return out


# --- 1. locate and dump the embedded cabinet ---------------------------
ole = olefile.OleFileIO(MSI)
streams = ["/".join(p) for p in ole.listdir()]
# The MSI obfuscates stream names, so identify the cabinet by its 'MSCF'
# magic rather than by name.
cab_files = {}
for s in streams:
    try:
        size = ole.get_size(s)
    except Exception:
        continue
    if size < 1024:
        continue
    with ole.openstream(s) as st:
        magic = st.read(4)
    if magic != b"MSCF":
        continue
    data = ole.openstream(s).read()
    path = os.path.join(OUT, "media1.cab")
    with open(path, "wb") as f:
        f.write(data)
    cab_files["media1.cab"] = path
    print(f"cab stream {ascii(s)}: {len(data)} bytes -> {path}")

if not cab_files:
    sys.exit("no embedded cabinet found")

# --- 2. expand the cabinets -------------------------------------------
expanded = []
for name, path in cab_files.items():
    dest = os.path.join(OUT, "_expanded", os.path.splitext(name)[0])
    os.makedirs(dest, exist_ok=True)
    subprocess.run(["expand.exe", "-F:*", path, dest], check=True,
                   stdout=subprocess.DEVNULL)
    expanded.append(dest)
    print(f"expanded {name} -> {dest}")


# --- 3. rebuild the tree from the MSI tables ---------------------------
db = msilib.OpenDatabase(MSI, 1)

file_rows = [(r[0], r[1], r[2])
             for r in msi_rows(db, "SELECT `File`,`Component_`,`FileName` FROM `File`", 3)]
comp_dir = {r[0]: r[1]
            for r in msi_rows(db, "SELECT `Component`,`Directory_` FROM `Component`", 2)}
dir_parent = {r[0]: (r[1], r[2])
              for r in msi_rows(db, "SELECT `Directory`,`Directory_Parent`,`DefaultDir` FROM `Directory`", 3)}
print(f"file_rows={len(file_rows)} comp_dir={len(comp_dir)} dir_parent={len(dir_parent)}")
print("sample dir_parent:", list(dir_parent.items())[:4])


def long_name(fn):
    return fn.split("|", 1)[1] if "|" in fn else fn


def short_name(fn):
    return fn.split("|", 1)[0] if "|" in fn else fn


def folder_name(dir_id, default):
    if "|" in default:
        default = default.split("|", 1)[1]
    if default and default != "SourceDir":
        return default
    if dir_id.startswith("CM_DP_server."):
        return dir_id[len("CM_DP_server."):].replace(".", os.sep)
    return ""


def dir_path(d):
    parts, seen = [], set()
    while d and d not in seen:
        seen.add(d)
        parent, default = dir_parent.get(d, ("", d))
        parts.append(folder_name(d, default))
        d = parent
    parts = [p for p in reversed(parts) if p]
    return os.path.join(*parts) if parts else ""


cab_index = {}
for dest in expanded:
    for entry in os.listdir(dest):
        cab_index.setdefault(entry.lower(), os.path.join(dest, entry))

missing, count = [], 0
for file_id, component, filename in file_rows:
    target = os.path.join(OUT, dir_path(comp_dir.get(component, "")),
                          long_name(filename))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    src = cab_index.get(short_name(filename).lower()) or \
        cab_index.get(long_name(filename).lower()) or \
        cab_index.get(file_id.lower())
    if src is None:
        missing.append((file_id, filename))
        continue
    if os.path.abspath(src) != os.path.abspath(target):
        if os.path.exists(target):
            os.remove(target)
        os.replace(src, target)
    count += 1

print(f"placed {count} files under {OUT}")
if missing:
    print(f"MISSING {len(missing)}: {missing[:20]}")
