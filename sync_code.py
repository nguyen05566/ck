#!/usr/bin/env python3
"""Sync all ck*.py bot files to the newest 1002-line (v10) code, preserving cookie refs.

- Template = newest committed file among files >= 1000 lines (or TEMPLATE env).
- Each bot keeps its own SINGLE_COOKIE_FILE default (ck*.txt).
- Writes sync_report.txt with details (committed to repo for review).
"""
import os
import re
import glob
import subprocess
import difflib

DRY = os.environ.get("DRY_RUN", "true").lower() == "true"
TEMPLATE = os.environ.get("TEMPLATE", "auto").strip()

cookie_re = re.compile(
    r'(SINGLE_COOKIE_FILE\s*=\s*os\.environ\.get\("SINGLE_COOKIE_FILE",\s*")([^"]*)("\))'
)

files = sorted(glob.glob("ck*.py"), key=lambda f: int(re.search(r"\d+", f).group()))
data = {}
for f in files:
    src = open(f, encoding="utf-8").read()
    m = cookie_re.search(src)
    cookie = m.group(2) if m else None
    lines = len(src.splitlines())
    norm = cookie_re.sub(lambda mm: mm.group(1) + "<<<COOKIE>>>" + mm.group(3), src) if m else src
    data[f] = dict(src=src, cookie=cookie, lines=lines, norm=norm)

new_files = [f for f in files if data[f]["lines"] >= 1000]
old_files = [f for f in files if data[f]["lines"] < 1000]


def last_commit(f):
    try:
        out = subprocess.check_output(["git", "log", "-1", "--format=%ct", "--", f], text=True).strip()
        return int(out or 0)
    except Exception:
        return 0


if TEMPLATE != "auto" and TEMPLATE in data:
    canonical = TEMPLATE
elif new_files:
    canonical = max(new_files, key=last_commit)
else:
    canonical = files[0]

base = data[canonical]["norm"]

report = []
report.append(f"DRY_RUN={DRY} TEMPLATE={TEMPLATE} canonical={canonical} ({data[canonical]['lines']} lines)")
report.append("")
report.append("=== Files ===")
for f in files:
    tag = "NEW" if data[f]["lines"] >= 1000 else "OLD"
    same = "base" if data[f]["norm"] == base else "VARIANT"
    report.append(f"{f:10s} {tag} lines={data[f]['lines']:5d} cookie={data[f]['cookie']} {same}")

groups = {}
for f in new_files:
    groups.setdefault(data[f]["norm"], []).append(f)
report.append("")
report.append(f"=== Variant groups among NEW files: {len(groups)} ===")
if len(groups) > 1:
    gl = sorted(groups.values(), key=len, reverse=True)
    report.append(f"largest group ({len(gl[0])} files): {gl[0]}")
    for g in gl[1:]:
        report.append(f"other group ({len(g)} files): {g}")
    other = gl[1][0]
    diff = list(difflib.unified_diff(
        data[canonical]["norm"].splitlines(), data[other]["norm"].splitlines(),
        fromfile=canonical, tofile=other, lineterm=""))
    report.append("")
    report.append(f"=== Diff {canonical} vs {other} (max 150 lines) ===")
    report.extend(diff[:150])

report.append("")
if DRY:
    report.append("=== DRY RUN — no bot file changed ===")
else:
    n = 0
    for f in files:
        cookie = data[f]["cookie"] or f.replace(".py", ".txt")
        new_src = base.replace("<<<COOKIE>>>", cookie)
        if new_src != data[f]["src"]:
            open(f, "w", encoding="utf-8").write(new_src)
            n += 1
            report.append(f"REWRITE {f}: {data[f]['lines']} -> {len(new_src.splitlines())} lines, cookie={cookie}")
        else:
            report.append(f"OK      {f}: unchanged, cookie={cookie}")
    report.append(f"Total rewritten: {n}")

open("sync_report.txt", "w", encoding="utf-8").write("\n".join(report) + "\n")
print("\n".join(report))
