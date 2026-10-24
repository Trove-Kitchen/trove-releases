"""Names the Maestro recordings that may become clear previews, one per line. Fails closed.

Only these become previews on the flow board (renders.py publish); every recording also stays in the encrypted
archive. A recording (the commands between startRecording and the next stopRecording) is previewable only when
everything it runs is understood and none of it can type the test password:
- every ${...} it uses names only ALLOWED variables, and no flow anywhere assigns one of those from anything else;
- it runs no script (runScript, evalScript);
- every subflow it runs (runFlow or file:, .yaml or .yml, quoted or not) is inside the flows dir and passes the same
  checks, recursively; a subflow that mentions the password, or one it can't read, makes the recording unsafe.
Run on the device runner: python3 -I tools/previewable.py <trove-app flows dir>
"""
from pathlib import Path
import re
import sys

ALLOWED = {"APP_ID", "TROVE_TEST_EMAIL", "RECIPE_TITLE", "RECIPE_URL", "GROCERY_ITEM", "COLLECTION"}
START = re.compile(r"\s*-\s*startRecording\b")
NAME = re.compile(r"\s*-\s*startRecording:\s*([A-Za-z0-9_-]+)\s*$")
STOP = re.compile(r"\s*-\s*stopRecording\b")
SCRIPT = re.compile(r"\b(runScript|evalScript)\b")
SUBFLOW = re.compile(r"\b(?:runFlow|file)\s*:\s*['\"]?([^'\"\s#]+)")
INTERP = re.compile(r"\$\{([^}]*)\}")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def names(expr: str) -> set[str]:
    return set(WORD.findall(QUOTED.sub("", expr)))


def line_ok(line: str) -> bool:
    if SCRIPT.search(line) or "TROVE_TEST_PASSWORD" in line:
        return False
    return all(names(expr) <= ALLOWED for expr in INTERP.findall(line))


class Checker:
    def __init__(self, flows: Path):
        self.root = flows.resolve()
        self.files: dict[Path, bool] = {}

    def subflow_ok(self, here: Path, ref: str) -> bool:
        if not ref.endswith((".yaml", ".yml")):
            return True  # `runFlow:` followed by an inline block (when:/commands:) on the next lines
        path = (here.parent / ref).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return False
        if path not in self.files:
            self.files[path] = False  # a cycle counts as unsafe until proven otherwise
            self.files[path] = self.lines_ok(path, path.read_text().splitlines())
        return self.files[path]

    def lines_ok(self, here: Path, lines: list[str]) -> bool:
        for line in lines:
            if not line_ok(line):
                return False
            sub = SUBFLOW.search(line)
            if sub and not self.subflow_ok(here, sub.group(1)):
                return False
        return True

    def assignments_ok(self) -> bool:
        """No flow may set an ALLOWED variable from anything outside ALLOWED (e.g. GROCERY_ITEM: ${TROVE_TEST_PASSWORD})."""
        for path in self.root.rglob("*"):
            if path.suffix not in (".yaml", ".yml") or not path.is_file():
                continue
            for line in path.read_text().splitlines():
                m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
                if m and m.group(1) in ALLOWED and "${" in m.group(2) and not line_ok(m.group(2)):
                    return False
        return True

    def previewable(self) -> list[str]:
        if not self.assignments_ok():
            return []
        safe = []
        for flow in sorted(p for p in self.root.iterdir() if p.suffix in (".yaml", ".yml") and p.name != "config.yaml"):
            block, name = None, None
            for line in flow.read_text().splitlines():
                if START.match(line):
                    m = NAME.match(line)
                    name, block = (m.group(1) if m else None), []
                elif STOP.match(line):
                    if name and block is not None and self.lines_ok(flow, block):
                        safe.append(name)
                    block, name = None, None
                elif block is not None:
                    block.append(line)
            # A recording still open at the end of the flow ends there.
            if name and block is not None and self.lines_ok(flow, block):
                safe.append(name)
        return safe


if __name__ == "__main__":
    print("\n".join(Checker(Path(sys.argv[1])).previewable()))
