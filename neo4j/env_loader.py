"""
Load `.env` from the repository root (parent directory of `AITown_new`).

Scripts run from subfolders such as `AITown_new/test` have cwd ≠ repo root;
`load_dotenv()` alone would miss the file and fall back to wrong defaults.
"""

import os
import sys

from dotenv import load_dotenv


def _harden_stdio() -> None:
    """日志里的 ⚠️ / 中文在 GBK 等窄编码的管道或文件上 print 会抛 UnicodeEncodeError，
    把调用它的决策整个搞崩。改成替换无法编码的字符：输出可能难看，但逻辑不受影响。
    （utility/env_loader.py 里有同样一份：哪个先被导入都生效。）"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


_harden_stdio()


def load_project_env() -> None:
    _here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(_here, '..'))
    load_dotenv(os.path.join(repo_root, '.env'))
    load_dotenv()
