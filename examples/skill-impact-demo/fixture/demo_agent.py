from pathlib import Path

EXPECTED = (
    "Create `skill-demo-output.txt` containing exactly `skill enabled` "
    "followed by a newline."
)
skill_files = sorted(Path(".agents/skills").glob("*/SKILL.md"))
if len(skill_files) != 1:
    print("No single demo skill is installed; leaving the task unchanged.")
    raise SystemExit(0)

skill_text = skill_files[0].read_text(encoding="utf-8")
if EXPECTED not in skill_text:
    print("The installed skill does not contain the expected deterministic instruction.")
    raise SystemExit(2)

Path("skill-demo-output.txt").write_text("skill enabled\n", encoding="utf-8")
print("Applied the installed demo skill.")
