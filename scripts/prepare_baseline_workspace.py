"""Index verified baseline membership for local Firestore reads. No model or review writes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.baseline_workspace import prepare
from backend.config import Settings
from backend.repository import Repository

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(Repository(Settings.load()), apply=args.apply), indent=2))
