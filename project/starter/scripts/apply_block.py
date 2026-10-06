"""
Replace one or more sections of src/agent_orchestrator.py with the provided blocks.

Usage (from project/starter):
    python scripts/apply_block.py path/to/block_file.py

Each section of the provided file starts with a banner, for example:
    # ───────────────────────────────────────────────────────
    #  2.C - POLICY AGENT - MULTI-AGENT RAG
    # ───────────────────────────────────────────────────────
or
    # ═══════════════════════════════════════════════════════
    #  TASK 4 - MEMORY
    # ═══════════════════════════════════════════════════════
Recognized titles start with "#  2." or "#  TASK ". In the target file, a section runs
from its banner up to the next full banner (a "# ───" / "# ═══" separator line followed
by a "#  TITLE" line), excluded. A backup src/agent_orchestrator.py.bak is written first, then the file is
compiled to catch any syntax error.
"""
import pathlib
import re
import py_compile
import shutil
import sys

TARGET = pathlib.Path('src/agent_orchestrator.py')
TITLE_PREFIXES = ('#  2.', '#  TASK ')
BANNER = re.compile(r'\n# [─═]{10,}\n#  \S')


def _split_sections(block: str) -> list:
    """
    Split the provided file into sections.

    Args:
        block: Content of the provided file

    Returns:
        List of (title, full section text including its banner) tuples
    """
    lines = block.splitlines(keepends=True)
    starts = [i - 1 for i, line in enumerate(lines)
              if line.startswith(TITLE_PREFIXES) and i > 0]
    sections = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        title = lines[start + 1].strip()
        sections.append((title, ''.join(lines[start:end]).strip('\n')))
    return sections


def _replace_section(src: str, title: str, new_text: str) -> tuple:
    """
    Replace the section with the given title in src by new_text.

    Args:
        src:      Current content of the target file
        title:    Section title (e.g. "#  TASK 4 - MEMORY")
        new_text: New section, banner included

    Returns:
        Tuple (new content, size of the replaced section)
    """
    if src.count(title) != 1:
        sys.exit(f"Title '{title}' found {src.count(title)} times in {TARGET} (expected 1).")
    pos = src.index(title)
    start = src.rfind('\n', 0, src.rfind('\n', 0, pos)) + 1
    after_banner = src.index('\n', src.index('\n', pos) + 1) + 1
    # The section ends at the next full banner: a separator line (10+ box-drawing
    # characters) immediately followed by a "#  TITLE" line. Short "# --- note ---"
    # comments inside a section are not banners.
    match = BANNER.search(src, after_banner)
    if not match:
        sys.exit(f"No banner found after '{title}'.")
    end = match.start() + 1
    return src[:start] + new_text + '\n\n\n' + src[end:], end - start


def main() -> None:
    """Apply every section of the file given on the command line."""
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/apply_block.py <block_file.py>")
    block = pathlib.Path(sys.argv[1]).expanduser().read_text()
    sections = _split_sections(block)
    if not sections:
        sys.exit("No '#  2.X - ...' or '#  TASK N - ...' banner found in the block file.")

    src = TARGET.read_text()
    shutil.copy(TARGET, TARGET.with_suffix('.py.bak'))
    for title, text in sections:
        src, old_size = _replace_section(src, title, text)
        print(f"OK: section '{title}' replaced ({old_size} -> {len(text) + 1} characters).")
    TARGET.write_text(src)
    py_compile.compile(str(TARGET), doraise=True)
    print(f"File compiles. Backup: {TARGET.with_suffix('.py.bak')}")


if __name__ == '__main__':
    main()
