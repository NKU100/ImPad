"""Copy the frozen migration corpus without repository-grown acceptance records."""
import shutil
from pathlib import Path


def copy_migration_baseline(source: Path, destination: Path):
    return shutil.copytree(source, destination,
                           ignore=shutil.ignore_patterns('acceptance-v2.json', 'acceptance-evidence'))
