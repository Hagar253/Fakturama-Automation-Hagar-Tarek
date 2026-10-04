"""Dump the full UIA tree of the Fakturama window to a text file.

Usage:
    python inspect/dump_tree.py [output.md]
"""

import io
import os
import sys
from pywinauto import Desktop


def dump_tree(win, out: io.TextIOBase, max_depth: int = 25) -> int:
    count = 0

    def walk(elem, depth):
        nonlocal count
        if depth > max_depth:
            return
        pad = "    " * depth
        try:
            name = elem.window_text() or ""
            ctrl = getattr(elem, "element_info", None)
            if ctrl is None:
                return
            ct = ctrl.control_type
            try:
                aid = elem.element_info.auto_id
            except Exception:
                aid = None
            cls = elem.element_info.class_name
            rect = elem.rectangle()
            status = ""
            try:
                if "Edit" in str(ct):
                    status += f" value={elem.get_value()!r}"
            except Exception:
                pass
        except Exception as e:
            out.write(f"{pad}[error] {e!r}\n")
            return

        out.write(
            f"{pad}{ct:<12} name={name!r} auto_id={aid} class={cls!r}\n"
            f"{pad}    rect=(L{rect.left},T{rect.top},R{rect.right},B{rect.bottom}){status}\n"
        )
        count += 1
        try:
            children = elem.children()
        except Exception:
            children = []
        for ch in children:
            walk(ch, depth + 1)

    walk(win, 0)
    return count


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(script_dir, "fakturama_tree.txt")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    windows = Desktop(backend="uia").windows()
    target = None
    other_top_level = []

    for w in windows:
        title = w.window_text()
        if title.startswith("Fakturama") and ("C:\\" in title or title == "Fakturama"):
            target = w
        elif title and "fakturama" not in title.lower():
            continue  # skip unrelated apps (VS Code, Chrome, etc.)
        elif title:
            other_top_level.append(w)

    if target is None:
        print("Fakturama main window not found.")
        return 1

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# Fakturama Complete UIA Tree\n\n")
        f.write("## Main window (includes any EMBEDDED dialogs/popups still open)\n\n")

        # IMPORTANT: this single walk from the root already includes any
        # embedded Window controls (e.g. 'Select the address' was found
        # this way) -- they are part of the same tree, not separate.
        n = dump_tree(target, f)

        # Separately, ALSO check for genuinely separate top-level popup
        # windows -- some native widgets (e.g. combo/dropdown lists) render
        # as transient top-level OS windows instead of embedded controls.
        for w in other_top_level:
            f.write(f"\n\n--- SEPARATE TOP-LEVEL WINDOW: {w.window_text()!r} ---\n")
            win_spec = Desktop(backend="uia").window(handle=w.handle)
            n += dump_tree(win_spec, f)

        f.write(f"\n# total elements: {n}\n")

    print(f"Wrote {n} elements to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())