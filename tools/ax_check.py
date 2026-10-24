"""Summarises the goal screens' accessibility trees (AXe describe-ui JSON).

A button counts as library-drawn when it, or an element around it, carries an
identifier starting with "trove." (library components mark themselves, and the
spec's exceptions use "trove.exception."), or it sits in the system's tab bar or
navigation bar. Prints one line per screen; never prints source.
"""
import json, os, sys

out = sys.argv[1]
KEYS_ID = ("AXUniqueId", "identifier", "AXIdentifier", "accessibilityIdentifier")
KEYS_TYPE = ("type", "role", "AXRole", "role_description", "elementType")
KEYS_LABEL = ("AXLabel", "label", "title", "AXValue")
CHROME_TYPES = ("TabBar", "NavigationBar", "Toolbar")

def text(node, keys):
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v:
            return v
    return ""

def walk(node, marked, chrome, found):
    if isinstance(node, list):
        for n in node:
            walk(n, marked, chrome, found)
        return
    if not isinstance(node, dict):
        return
    ident = text(node, KEYS_ID)
    kind = text(node, KEYS_TYPE)
    marked = marked or ident.startswith("trove.")
    chrome = chrome or any(c in kind for c in CHROME_TYPES)
    if "Button" in kind or "Link" in kind:
        found.append((text(node, KEYS_LABEL), ident, marked or chrome))
    for key in ("children", "AXChildren", "elements"):
        if key in node:
            walk(node[key], marked, chrome, found)

first = True
for name in sorted(f for f in os.listdir(out) if f.endswith(".ax.json")):
    try:
        tree = json.load(open(os.path.join(out, name)))
    except Exception as error:
        print(f"{name}: unreadable ({error.__class__.__name__})")
        continue
    if first:
        sample = tree[0] if isinstance(tree, list) and tree else tree
        print("AXe fields:", sorted(sample.keys()) if isinstance(sample, dict) else type(sample).__name__)
        first = False
    found = []
    walk(tree, False, False, found)
    strays = [f"{label or '?'}{' #' + ident if ident else ''}" for label, ident, ok in found if not ok]
    print(f"{name[:-8]}: {len(found)} buttons, {len(found) - len(strays)} marked" + (f"; unmarked: {', '.join(strays[:12])}" if strays else ""))
