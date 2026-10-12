#!/usr/bin/env python3
"""Package a verified root image and boot tree without copying userdata templates twice."""
import argparse,hashlib,json,zipfile
from pathlib import Path
def main():
 ap=argparse.ArgumentParser(description=__doc__)
 for n in ['rootfs','boot','output']:ap.add_argument('--'+n,type=Path,required=True)
 a=ap.parse_args();source=Path(__file__).resolve().parents[1]
 if a.output.exists():ap.error('Output already exists')
 files=[(p,p.relative_to(a.boot/'sd-card').as_posix()) for p in (a.boot/'sd-card').rglob('*') if p.is_file()]
 files.append((a.rootfs,'switchroot/switchdeck-alpha/rootfs.squashfs'))
 records=[]
 for p,n in files:
  with p.open('rb') as f:h=hashlib.file_digest(f,'sha256').hexdigest()
  records.append(dict(path=n,bytes=p.stat().st_size,sha256=h))
 manifest=dict(revision='Alpha 1',hardware_boot_tested=False,files=records)
 a.output.parent.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(a.output,'x',allowZip64=True) as z:
  for p,n in files:z.write(p,'sd-card/'+n,compress_type=zipfile.ZIP_STORED)
  z.write(source/'alpha/README-RU.md','README-RU.md',compress_type=zipfile.ZIP_DEFLATED)
  z.writestr('image-manifest.json',json.dumps(manifest,indent=2)+'\n',compress_type=zipfile.ZIP_DEFLATED)
  z.writestr('sd-card/switchroot/switchdeck-alpha/wifi.example.txt','YOUR_WIFI_SSID\nYOUR_WIFI_PASSWORD\n')
  z.writestr('SHA256SUMS.txt',''.join(r['sha256']+'  sd-card/'+r['path']+'\n' for r in records))
 with zipfile.ZipFile(a.output) as z:
  bad=z.testzip()
  if bad:raise ValueError('ZIP CRC failed: '+bad)
 with a.output.open('rb') as f:h=hashlib.file_digest(f,'sha256').hexdigest()
 report=dict(archive=a.output.name,bytes=a.output.stat().st_size,sha256=h,zip_crc_passed=True,manifest=manifest)
 a.output.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({k:v for k,v in report.items() if k!='manifest'},indent=2))
if __name__=='__main__':main()
