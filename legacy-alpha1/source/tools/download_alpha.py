#!/usr/bin/env python3
"""Fetch pinned Alpha inputs into an explicitly selected temporary directory."""
import argparse,hashlib,json,urllib.request,concurrent.futures
from pathlib import Path
ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
a.output.mkdir(parents=True,exist_ok=True)
records=json.loads((Path(__file__).resolve().parents[1]/'alpha/download-lock.json').read_text())
def fetch(r):
 p=a.output/r['name']
 if not p.exists():
  tmp=p.with_suffix(p.suffix+'.partial')
  with urllib.request.urlopen(r['url'],timeout=90) as src,tmp.open('wb',buffering=0) as dst:
   while b:=src.read(2**20):
    if dst.write(b)!=len(b):raise RuntimeError('Short write: '+p.name)
  tmp.rename(p)
 with p.open('rb') as f:h=hashlib.file_digest(f,'sha256').hexdigest()
 if h!=r['sha256'] or p.stat().st_size!=r['bytes']:raise ValueError('Integrity mismatch: '+p.name)
 print('Verified',p.name,flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(fetch,records))
