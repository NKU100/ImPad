import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


def accepted_example(app_id):
    path = _ROOT / 'compatibility' / app_id / 'acceptance-v2.json'
    return json.loads(path.read_bytes())['records'][0]
