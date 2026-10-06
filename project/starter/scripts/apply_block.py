"""
Remplace une section « 2.X » de src/agent_orchestrator.py par un bloc fourni.

Usage (depuis project/starter) :
    python scripts/apply_block.py ~/Downloads/etape2c_policy_agent.py

Le bloc doit commencer par la bannière de section, par exemple :
    # ───────────────────────────────────────────────────────
    #  2.C - POLICY AGENT - MULTI-AGENT RAG
    # ───────────────────────────────────────────────────────
La section remplacée va de cette bannière jusqu'à la bannière suivante
(ligne commençant par « # ─ » ou « # ═ » en colonne 0), exclue.
Une sauvegarde src/agent_orchestrator.py.bak est créée avant écriture,
puis le fichier est compilé pour détecter toute erreur de syntaxe.
"""
import pathlib
import py_compile
import shutil
import sys

TARGET = pathlib.Path('src/agent_orchestrator.py')


def main() -> None:
    """Applique le bloc passé en argument à la section correspondante."""
    if len(sys.argv) != 2:
        sys.exit("Usage : python scripts/apply_block.py <fichier_bloc.py>")
    block = pathlib.Path(sys.argv[1]).expanduser().read_text()

    # Titre de section = première ligne « #  2.X - ... » du bloc
    title = next((line.strip() for line in block.splitlines()
                  if line.startswith('#  2.')), None)
    if not title:
        sys.exit("Bannière « #  2.X - ... » introuvable dans le bloc.")

    src = TARGET.read_text()
    if src.count(title) != 1:
        sys.exit(f"Titre « {title} » trouvé {src.count(title)} fois dans {TARGET} (attendu : 1).")

    pos = src.index(title)
    start = src.rfind('\n', 0, src.rfind('\n', 0, pos)) + 1   # ligne de séparateur avant le titre
    after_banner = src.index('\n', src.index('\n', pos) + 1) + 1  # après le séparateur de fermeture
    ends = [i for i in (src.find('\n# ─', after_banner), src.find('\n# ═', after_banner)) if i != -1]
    if not ends:
        sys.exit("Bannière de section suivante introuvable.")
    end = min(ends) + 1

    shutil.copy(TARGET, TARGET.with_suffix('.py.bak'))
    TARGET.write_text(src[:start] + block.strip('\n') + '\n\n\n' + src[end:])
    py_compile.compile(str(TARGET), doraise=True)
    print(f"OK : section « {title} » remplacée ({end - start} → {len(block)} caractères). "
          f"Sauvegarde : {TARGET.with_suffix('.py.bak')}")


if __name__ == '__main__':
    main()
