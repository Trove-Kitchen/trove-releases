"""Names the Maestro recordings that never film the test password being typed, one per line.

Only these become clear previews on the flow board (renders.py publish); every recording stays in the encrypted
archive as well. A recording is unsafe when, between its startRecording and the next stopRecording, its flow types
TROVE_TEST_PASSWORD itself or runs a subflow that mentions it. Any startRecording this can't read is unsafe too.
Run on the device runner: python3 -I tools/previewable.py <trove-app flows dir>
"""
from pathlib import Path
import re
import sys

NAME = re.compile(r"\s*-\s*startRecording:\s*([A-Za-z0-9._-]+)\s*$")


def previewable(flows: Path) -> list[str]:
    secret = {p.resolve() for p in flows.rglob("*.yaml") if "TROVE_TEST_PASSWORD" in p.read_text()}
    safe, unsafe = [], set()
    for flow in sorted(flows.glob("*.yaml")):
        current = None
        for line in flow.read_text().splitlines():
            if re.match(r"\s*-\s*startRecording\b", line):
                m = NAME.match(line)
                current = m.group(1) if m else "?"
                safe.append(current)
            elif re.match(r"\s*-\s*stopRecording\b", line):
                current = None
            elif current:
                sub = re.search(r"(?:runFlow:|file:)\s*([A-Za-z0-9._/-]+\.yaml)", line)
                if "TROVE_TEST_PASSWORD" in line or (sub and (flow.parent / sub.group(1)).resolve() in secret):
                    unsafe.add(current)
    return [n for n in safe if n != "?" and n not in unsafe]


if __name__ == "__main__":
    print("\n".join(previewable(Path(sys.argv[1]))))
