"""Insert the Analysis section into evidence/standout/guardrail_adversarial.md."""
import pathlib
import sys

report = pathlib.Path('evidence/standout/guardrail_adversarial.md')
analysis = pathlib.Path(sys.argv[1]).expanduser().read_text().strip()
text = report.read_text()
placeholder = "## Analysis\n\n_To be completed after review of the responses below._"
if placeholder not in text:
    sys.exit("Placeholder not found: the Analysis section was already inserted.")
report.write_text(text.replace(placeholder, analysis))
print("Analysis section inserted.")
