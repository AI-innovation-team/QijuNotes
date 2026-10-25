#!/usr/bin/env python3
"""Read real observation logs without initialising or modifying the database."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dsh_personal.observation import observation_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default=str(Path.home() / '.local/share/dsh-personal'))
    args = parser.parse_args()
    report = observation_report(args.data)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get('read_error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
