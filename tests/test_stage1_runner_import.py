"""Custom subprocess planners should not load the unused default planner."""
import subprocess
import sys
from pathlib import Path


def test_custom_stage1_runner_does_not_import_the_default_planner():
    subprocess.run(
        [sys.executable, "-c",
         "import sys; import api.generation.stage1_runner; "
         "assert 'fightcamp.main' not in sys.modules"],
        cwd=Path.cwd(), check=True, capture_output=True, text=True,
    )
