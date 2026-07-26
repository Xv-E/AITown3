"""
Load `.env` from the repository root (parent directory of `AITown_new`).

Scripts run from subfolders such as `AITown_new/test` have cwd ≠ repo root;
`load_dotenv()` alone would miss the file and fall back to wrong defaults.
"""

import os
from dotenv import load_dotenv


def load_project_env() -> None:
    _here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(_here, '..'))
    load_dotenv(os.path.join(repo_root, '.env'))
    load_dotenv()
