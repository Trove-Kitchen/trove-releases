"""Names the Playwright specs whose videos may become clear previews, one file stem per line. Fails closed.

A spec is previewable only when neither it nor any local module it imports (relative imports, followed
transitively) mentions a password. The web flows sign in once in an unrecorded setup project and hand the
session to the recorded tests, so recorded specs never touch the password. A spec that does, or one whose
imports can't be read, gets no preview; every video still goes to the encrypted archive.
Run on the web runner: python3 -I tools/previewable_web.py <trove-app web/flows dir>
"""
from pathlib import Path
import re
import sys

IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""")
SECRET = re.compile(r"password", re.IGNORECASE)


def clean(path: Path, root: Path, seen: set[Path]) -> bool:
    path = path.resolve()
    if path in seen:
        return True
    seen.add(path)
    if not path.is_relative_to(root) or not path.is_file():
        return False
    text = path.read_text()
    if SECRET.search(text):
        return False
    for ref in IMPORT.findall(text):
        target = (path.parent / ref)
        candidates = [target] if target.suffix else [target.with_suffix(s) for s in (".ts", ".tsx", ".js", ".mjs")]
        found = next((c for c in candidates if c.is_file()), None)
        if found is None or not clean(found, root, seen):
            return False
    return True


def previewable(flows: Path) -> list[str]:
    root = flows.resolve().parent  # web/: specs may import shared helpers next to flows/
    return [spec.name[: -len(".spec.ts")] for spec in sorted(flows.glob("*.spec.ts")) if clean(spec, root, set())]


if __name__ == "__main__":
    print("\n".join(previewable(Path(sys.argv[1]))))
