"""Runtime check of the iOS goal screens' buttons, from AXe describe-ui JSON.

Every button must be drawn by the component library or be one of the spec's
exceptions (docs/design/components.md in trove-app):
- library components mark themselves with a "trove.<Name>" accessibility
  identifier, which SwiftUI passes down to the elements inside them;
- exceptions drawn in the app mark themselves "trove.exception.<Kind>"
  (navigation chrome, glyph buttons, inline text links);
- the platform's own controls are recognised from the tree: the tab bar (the
  "Tab Bar" group), menus (pop-up buttons and the button inside one) and
  segmented controls (radio buttons in a tab group).
Hidden zero-size buttons are skipped. Prints names and counts only.
"""
import json, os, sys

out = sys.argv[1]

def walk(node, ctx, found):
    if isinstance(node, list):
        for child in node:
            walk(child, ctx, found)
        return
    if not isinstance(node, dict):
        return
    ident = node.get("AXUniqueId") or ""
    label = node.get("AXLabel") or ""
    kind = node.get("type") or ""
    ctx = dict(ctx)
    if ident.startswith("trove."):
        ctx["mark"] = ident
    if label == "Tab Bar":
        ctx["mark"] = "platform:tab bar"
    if kind == "PopUpButton":
        ctx["mark"] = "platform:menu"
    if kind == "TabGroup":
        ctx["tabgroup"] = True
    frame = node.get("frame") or {}
    if ("Button" in kind or "Link" in kind) and frame.get("width", 1) > 0 and frame.get("height", 1) > 0:
        mark = ctx.get("mark") or ("platform:segmented" if kind == "RadioButton" and ctx.get("tabgroup") else None)
        found.append((label or "?", mark))
    for child in node.get("children") or []:
        walk(child, ctx, found)

screens = buttons = unmarked = 0
for name in sorted(f for f in os.listdir(out) if f.endswith(".ax.json")):
    screen = name[: -len(".ax.json")]
    try:
        tree = json.load(open(os.path.join(out, name)))
    except Exception:
        print(f"{screen}: no accessibility tree")
        continue
    found = []
    walk(tree, {}, found)
    strays = [label for label, mark in found if not mark]
    kinds = {}
    for _, mark in found:
        if mark:
            key = mark.split(".")[1] if mark.startswith("trove.") and not mark.startswith("trove.exception.") else mark.replace("trove.", "")
            kinds[key] = kinds.get(key, 0) + 1
    screens += 1
    buttons += len(found)
    unmarked += len(strays)
    print(f"{screen}: {len(found)} buttons, {len(strays)} unmarked" + (f" ({', '.join(strays)})" if strays else "")
          + " | " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
print(f"IOS SCREEN CHECK: {screens} screens, {buttons} buttons, {unmarked} not drawn by the library")
