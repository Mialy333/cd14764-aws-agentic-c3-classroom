"""
Insert an Analysis section into an adversarial report written by adversarial_test.py.

Usage (from project/starter):
    python scripts/apply_analysis.py analysis.md                                    # default report
    python scripts/apply_analysis.py analysis.md evidence/standout/other_report.md  # chosen report
"""
import pathlib
import sys

if len(sys.argv) not in (2, 3):
    sys.exit("Usage: python scripts/apply_analysis.py <analysis.md> [report.md]")
analysis = pathlib.Path(sys.argv[1]).expanduser().read_text().strip()
report = pathlib.Path(sys.argv[2] if len(sys.argv) == 3 else 'evidence/standout/guardrail_adversarial.md')
text = report.read_text()
placeholder = "## Analysis\n\n_To be completed after review of the responses below._"
if placeholder not in text:
    sys.exit(f"Placeholder not found in {report}: the Analysis section was already inserted.")
report.write_text(text.replace(placeholder, analysis))
print(f"Analysis section inserted into {report}.")
