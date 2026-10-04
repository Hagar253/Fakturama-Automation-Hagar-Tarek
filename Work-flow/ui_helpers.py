import time

from pywinauto import Desktop
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError

from config import WINDOW_TITLE_PREFIX, ACTION_TIMEOUT, POLL_INTERVAL


def wait_until(condition_fn, timeout=ACTION_TIMEOUT, interval=POLL_INTERVAL, description="condition"):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            result = condition_fn()
            if result:
                return result
        except Exception as e:  
            last_error = e
        time.sleep(interval)
    raise RuntimeError(f"Timed out waiting for: {description}. Last error: {last_error}")


def find_fakturama_window():
    for w in Desktop(backend="uia").windows():
        if w.window_text().startswith(WINDOW_TITLE_PREFIX): 
            win_spec = Desktop(backend="uia").window(handle=w.handle)
            win_spec.set_focus()
            time.sleep(0.5)
            return win_spec
    raise RuntimeError(f"No window starting with '{WINDOW_TITLE_PREFIX}' found. Is Fakturama open?")


def find_edit_right_of_label(container, label_text, vertical_tolerance=15):
    """Finds the Edit box to the right of a Text label. UIA reports these
    labels as 'Text', even though Win32 calls them 'Static'."""
    label = container.child_window(title=label_text, control_type="Text")
    label.wait("exists visible", timeout=ACTION_TIMEOUT)
    lrect = label.rectangle()
    label_vcenter = (lrect.top + lrect.bottom) / 2

    best, best_dist = None, None
    for edit in container.descendants(control_type="Edit"):
        r = edit.rectangle()
        vcenter = (r.top + r.bottom) / 2
        if abs(vcenter - label_vcenter) <= vertical_tolerance and r.left >= lrect.right - 5:
            dist = r.left - lrect.right
            if best is None or dist < best_dist:
                best, best_dist = edit, dist
    if best is None:
        raise RuntimeError(f"Could not find an Edit control next to label '{label_text}'")
    return best


def find_edits_right_of_label(container, label_text, count, vertical_tolerance=15):
    """Finds several Edits sharing one label (e.g. 'ZIP - City').
    Returns them left to right."""
    label = container.child_window(title=label_text, control_type="Text")
    label.wait("exists visible", timeout=ACTION_TIMEOUT)
    lrect = label.rectangle()
    label_vcenter = (lrect.top + lrect.bottom) / 2

    candidates = []
    for edit in container.descendants(control_type="Edit"):
        r = edit.rectangle()
        vcenter = (r.top + r.bottom) / 2
        if abs(vcenter - label_vcenter) <= vertical_tolerance and r.left >= lrect.right - 5:
            candidates.append(edit)

    candidates.sort(key=lambda e: e.rectangle().left)
    if len(candidates) < count:
        raise RuntimeError(
            f"Expected {count} Edit controls next to label '{label_text}', found {len(candidates)}"
        )
    return candidates[:count]


def escape_for_type_keys(text):
    """type_keys() treats +^%~(){} as special characters. Wrapping each
    one in {} makes it type the literal character instead (otherwise '+49'
    comes out as '$9')."""
    special_chars = set("+^%~(){}")
    return "".join(f"{{{ch}}}" if ch in special_chars else ch for ch in text)


def set_field(edit_ctrl, value, label_for_log):
    """Uses type_keys() instead of set_text(), since set_text() doesn't
    trigger SWT's data-binding commit. Special characters are escaped
    first (see escape_for_type_keys)."""
    if not value:
        print(f"[OK] {label_for_log}: no value supplied, leaving as-is.")
        return

    edit_ctrl.set_focus()
    edit_ctrl.type_keys("^a{DELETE}", pause=0.05)
    edit_ctrl.type_keys(escape_for_type_keys(value), with_spaces=True, pause=0.02)
    edit_ctrl.type_keys("{TAB}")
    time.sleep(0.2)

    actual = read_edit_value(edit_ctrl)
    if value not in actual:
        raise RuntimeError(f"Verification failed for '{label_for_log}': expected '{value}', got '{actual}'")
    print(f"[OK] {label_for_log} set and verified (post-commit): '{actual}'")


def invoke_or_click(ctrl, description, prefer_click=False):
    """prefer_click=True skips invoke(). Some Image controls accept
    invoke() without error but do nothing, so clicking is safer."""
    if prefer_click:
        ctrl.click_input()
    else:
        try:
            ctrl.invoke()
        except Exception:
            ctrl.click_input()
    print(f"[OK] Triggered: {description}")

def read_edit_value(edit_ctrl):
    return edit_ctrl.get_value() if hasattr(edit_ctrl, "get_value") else edit_ctrl.window_text()

def switch_to_order_tab(win):
    """Finds the Order tab by position, not name -- Fakturama renames it
    when saving (e.g. '*New Order' -> '*PO000003'). Main tabs are:
    index 0 = 'Fakturama', index 1 = Order. Filtered by y-position to
    skip TabItems from dialogs."""
    tab_items = win.descendants(control_type="TabItem")
    main_tabs = [t for t in tab_items if t.rectangle().top < 200]
    main_tabs.sort(key=lambda t: t.rectangle().left)

    if len(main_tabs) < 2:
        raise RuntimeError(
            f"Expected at least 2 main-area tabs (Fakturama + Order), found {len(main_tabs)}."
        )

    order_tab = main_tabs[1]
    invoke_or_click(order_tab, f"Order tab ('{order_tab.window_text()}')", prefer_click=True)
    time.sleep(1.0)

def get_order_content_pane(win):
    " Grabs the pane so other functions can search inside it"
    pane = win.child_window(title="New Order", control_type="Pane")
    pane.wait("exists visible", timeout=ACTION_TIMEOUT)
    return pane

def ensure_order_tab_active(win):
    """Switches to the Order tab and returns a fresh order_pane.
    Always call this before touching Items/Addresses -- switching tabs
    can leave old element references pointing at stale state."""
    switch_to_order_tab(win)
    pane = get_order_content_pane(win)
    pane.set_focus()
    time.sleep(0.3)
    return pane

def _find_search_edit(dialog):
    """Anchors to the 'Search:' label"""
    search_label = dialog.child_window(title="Search:", control_type="Text")
    search_label.wait("exists visible", timeout=ACTION_TIMEOUT)
    lrect = search_label.rectangle()
    label_vcenter = (lrect.top + lrect.bottom) / 2

    for edit in dialog.descendants(control_type="Edit"):
        r = edit.rectangle()
        vcenter = (r.top + r.bottom) / 2
        if abs(vcenter - label_vcenter) <= 15 and r.left >= lrect.right - 5:
            return edit
    raise RuntimeError("Could not find the search Edit box next to 'Search:' label.")

def _get_grid_row_count(dialog):
    """Reads row count via UIA's GridPattern"""
    for pane in dialog.descendants(control_type="Pane"):
        try:
            grid_iface = pane.iface_grid
            count = grid_iface.CurrentRowCount
            if count is not None:
                return count, pane
        except Exception:
            continue
    return None, None

def _click_ok(dialog):
    ok_btn = dialog.child_window(title="OK", control_type="Button")
    ok_btn.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
    invoke_or_click(ok_btn, f"OK in '{dialog.window_text()}' dialog")


def _cancel_dialog(dialog):
    cancel_btn = dialog.child_window(title="Cancel", control_type="Button")
    cancel_btn.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
    invoke_or_click(cancel_btn, f"Cancel in '{dialog.window_text()}' dialog")

def click_nav_link_until_tab_opens(win, link_title, tab_title, max_attempts=4, settle_delay=0.6):
    """Sometimes it would click once and the tab doesn't open, so this function is for it to try clicking multiple times."""
    import re
    tab_title_pattern = re.compile(r"^\*?" + re.escape(tab_title) + r"$")

    for attempt in range(1, max_attempts + 1):
        win.set_focus()
        time.sleep(settle_delay)

        link = win.child_window(title=link_title, control_type="Text")
        link.wait("exists visible", timeout=ACTION_TIMEOUT)
        link.set_focus()
        link.click_input()

        def tab_opened():
            for tab_item in win.descendants(control_type="TabItem"):
                name = tab_item.window_text() or ""
                if tab_title_pattern.match(name):
                    return tab_item
            return None

        try:
            wait_until(tab_opened, timeout=3, description=f"'{tab_title}' (or '*{tab_title}') tab to appear (attempt {attempt})")
            print(f"[OK] '{tab_title}' tab opened/activated on attempt {attempt}.")
            return
        except RuntimeError:
            print(f"[WARN] Attempt {attempt}/{max_attempts}: tab did not appear yet, retrying click...")

    raise RuntimeError(
        f"'{tab_title}' tab never appeared after {max_attempts} click attempts on '{link_title}'."
    )

def _find_button_in_toolbar(win, title):
    candidates = win.descendants(control_type="Button")
    matched = []
    for b in candidates:
        try:
            name = b.window_text()
            if title.lower() in name.lower():
                matched.append(b)
        except Exception:
            continue

    if not matched:
        raise RuntimeError(f"Could not find any toolbar button matching '{title}'.")
    
    # Sort by vertical position 
    matched.sort(key=lambda b: b.rectangle().top)
    return matched[0]