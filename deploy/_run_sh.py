"""Run a Windows-authored shell script on the VPS with line endings fixed.

Usage: python3 _run_sh.py /tmp/script_raw.sh
"""
import pathlib
import subprocess
import sys

src = pathlib.Path(sys.argv[1])
dst = src.with_name(src.stem.removesuffix("_raw") + ".clean.sh")
dst.write_text(src.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", ""))
sys.exit(subprocess.run(["bash", str(dst)]).returncode)
