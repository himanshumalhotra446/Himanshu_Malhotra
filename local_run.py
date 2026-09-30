"""Run the scan on your own computer.
  python local_run.py            -> writes report.html (no mail sent)
  python local_run.py --send     -> sends the mail (needs SMTP_USER, SMTP_PASSWORD, MAIL_TO)
"""
import sys
from pathlib import Path
from ichimoku.core import run

send = "--send" in sys.argv
status, body, ctype = run(send=send, force=True)
if ctype.startswith("text/html"):
    Path("report.html").write_text(body, encoding="utf-8")
    print("Wrote report.html")
else:
    print(status, body)
