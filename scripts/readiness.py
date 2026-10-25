#!/usr/bin/env python3
"""Read local configuration and startup evidence without printing endpoints or secrets."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_personal.diagnostics import readiness
parser=argparse.ArgumentParser();parser.add_argument('--data',default=str(Path.home()/'.local/share/dsh-personal'))
args=parser.parse_args()
print(json.dumps(readiness(args.data),ensure_ascii=False,indent=2))
