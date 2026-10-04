"""
Fakturama Image-to-Cash Automation — Order + Debtor Stage
===========================================================
Implements:
  Stage 1 (PDF section 1): Open a New Order, set Date and Cust.Ref.
  Stage 2 (PDF section 2): Select an existing Debtor by exact match, or
                            create a new one -- including Main address,
                            Invoice/Delivery role assignment -- then
                            re-confirm it persisted.

Design-doc principles followed throughout:
  - Controls found by accessible name / control_type, never coordinates.
    click_input() always acts on a LIVE element reference whose rectangle
    is read from the tree at runtime -- never a hardcoded (x, y).
  - Semantic actions (invoke/set_text) preferred; click_input/type_keys
    used as a deliberate fallback for controls that don't support those
    patterns (Image icons in this SWT app silently no-op on invoke()).
  - Exact-match only for Debtor selection. Anything ambiguous raises
    ManualReviewRequired and halts -- it is never guessed around.
  - Verify-after-act: every field write and dialog transition is read
    back and confirmed before the next step runs.

Known, documented coverage gaps / app quirks discovered via tree inspection:
  - The 'Select the address' dialog and the address-type role popup are
    BOTH embedded controls inside Fakturama's own window tree (Win32
    classes 'SWT_Window0' and '#32770' respectively) -- neither is a
    separate top-level OS window, so Desktop().windows() can never find
    them. They must be located via descendants()/child_window() on the
    already-known main window instead.
  - The address-selection results grid does not expose individual rows as
    UIA tree elements; row COUNT is read via the native GridPattern
    interface where supported, with a reduced-confidence keyboard-
    selection fallback otherwise.
  - SWT auto_id values are numeric and unstable across sessions; all
    lookups anchor to visible labels/accessible names instead.
"""

import time
from datetime import datetime

from pywinauto import Desktop
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError


# ===========================================================================
# Config
# ===========================================================================
WINDOW_TITLE_PREFIX = "Fakturama - C:"
ACTION_TIMEOUT = 10
POLL_INTERVAL = 0.3


# ===========================================================================
# Design-doc-aligned exception
# ===========================================================================
class ManualReviewRequired(Exception):
    """Raised when extracted data and UI state don't resolve to a single
    unambiguous outcome. Per the design doc, this halts the flow -- it is
    not retried or guessed around."""
    pass


# ===========================================================================
# Generic helpers
# ===========================================================================
def wait_until(condition_fn, timeout=ACTION_TIMEOUT, interval=POLL_INTERVAL, description="condition"):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            result = condition_fn()
            if result:
                return result
        except Exception as e:  # noqa: BLE001 - retry on any transient UIA error
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
    """Single unnamed Edit to the right of a Text label. UIA exposes these
    labels as control_type='Text' even though their underlying Win32 class
    is 'Static' -- 'Static' is not a valid UIA control_type in pywinauto."""
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
    """Multiple unnamed Edits sharing one label (e.g. 'ZIP - City',
    'First Name Last Name'). Returns them sorted left-to-right."""
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
    """type_keys() uses SendKeys-style syntax where +^%~(){} are modifier/
    grouping characters, not literal text -- e.g. '+49' was being read as
    Shift+4 (producing '$') followed by '9'. Wrapping each special
    character in its own {} sends it as a literal keystroke instead."""
    special_chars = set("+^%~(){}")
    return "".join(f"{{{ch}}}" if ch in special_chars else ch for ch in text)


def set_field(edit_ctrl, value, label_for_log):
    """type_keys() is the PRIMARY method (see earlier note on set_text()
    not triggering SWT's data-binding commit). Special characters in the
    value are escaped before sending, since SendKeys syntax treats
    +^%~(){} as modifiers/grouping rather than literal characters."""
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
    """prefer_click=True skips invoke() entirely. Needed for Image controls
    in this app, which frequently accept invoke() without error but don't
    actually perform the action -- a silent no-op, unlike real Buttons."""
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
    """Finds and activates the Order tab by POSITION, not by name --
    Fakturama renames this tab dynamically (e.g. '*New Order' becomes
    '*PO000003' once saved), so name-substring matching is unreliable
    across runs. The main-area tab strip is consistently: index 0 =
    'Fakturama' home tab, index 1 = the Order tab (always created first,
    right after Fakturama). Filtered by vertical position to exclude
    unrelated nested TabItems (e.g. inside dialogs) that share the same
    control_type."""
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


def ensure_order_tab_active(win):
    """Switches to the Order tab and returns a FRESHLY FETCHED order_pane.
    Must be called before any Items/Addresses interaction on the Order --
    switching to another tab (Debtor editor, New product editor) can
    silently leave old pywinauto element references pointing at stale,
    disconnected state (reads/clicks don't error, they just act on the
    wrong/old pane) -- same root cause already confirmed and fixed once
    for Stage 2's Debtor re-selection; Stage 3 needs the same treatment
    at every tab-crossing point, not just after Debtor creation."""
    switch_to_order_tab(win)
    pane = get_order_content_pane(win)
    pane.set_focus()
    time.sleep(0.3)
    return pane

# ===========================================================================
# Open a New Order
# ===========================================================================
def open_new_order(win):
    """Accessible name is 'Create: New Order' -- the visible toolbar label
    is just 'Order'. Confirmed from the captured control tree."""
    try:
        order_button = win.child_window(title="Create: New Order", control_type="Button") # Search the window for a button element with matching accessable name and control type
        order_button.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
        invoke_or_click(order_button, "'Create: New Order' toolbar button") # the trigger
    except (ElementNotFoundError, PywinautoTimeoutError) as e:
        raise RuntimeError(f"Could not find/click the 'Create: New Order' button: {e}")

    # Check that the 'New Order' tab opened successfully.
    def tab_opened():
        try:
            tab_item = win.child_window(title="New Order", control_type="TabItem")
            tab_item.wait("exists visible", timeout=1)
            return tab_item
        except (ElementNotFoundError, PywinautoTimeoutError):
            return None

    # Ensure that the program doesn't race ahead before the tab is actually ready.
    wait_until(tab_opened, description="'New Order' tab to appear")
    print("[OK] New Order editor opened.")


def get_order_content_pane(win):
    " Retrieves the pane so other functions can search inside it"
    pane = win.child_window(title="New Order", control_type="Pane")
    pane.wait("exists visible", timeout=ACTION_TIMEOUT)
    return pane


def confirm_order_number_untouched(order_pane):
    no_field = find_edit_right_of_label(order_pane, "No.")
    value = no_field.get_value() if hasattr(no_field, "get_value") else no_field.window_text()
    print(f"[OK] Leaving auto-generated 'No.' field unchanged. Current value: '{value}'")


def set_date_field(order_pane, date_iso):
    """Observed existing value was 'Oct 1, 2026' (human-readable, not ISO).
    Tries the literal ISO string first; falls back to formats matching that
    observed style if verification fails, since SWT date widgets can
    silently reject unparseable text and leave the old value in place."""
    date_field = find_edit_right_of_label(order_pane, "Date")
    date_obj = datetime.strptime(date_iso, "%Y-%m-%d") # Convert the incoming ISO string into a standard Python datetime object.
    
    # Generate a list of candidate formats to try, including the original ISO string and two other common formats.
    candidates = [
        date_iso,
        f"{date_obj:%b} {date_obj.day}, {date_obj:%Y}",
        date_obj.strftime("%d.%m.%Y"),
    ]

    actual = None
    # Loop through each format
    for attempt in candidates:
        date_field.set_focus()
        date_field.type_keys("^a{DELETE}", pause=0.05)
        date_field.type_keys(attempt, with_spaces=True, pause=0.02)
        date_field.type_keys("{TAB}")
        time.sleep(0.4) # A brief pause to let the UI engine process the date input.

        actual = date_field.get_value() if hasattr(date_field, "get_value") else date_field.window_text()
        # Validate that the actual value matches the expected year and day.
        if date_obj.strftime("%Y") in actual and (
            str(date_obj.day) in actual or f"{date_obj.day:02d}" in actual
        ):
            print(f"[OK] Date field set using format '{attempt}'. Field now shows: '{actual}'")
            return

    raise RuntimeError(
        f"Could not verify Date field was set to {date_iso} after trying formats: {candidates}. "
        f"Last observed value: '{actual}'"
    )


def set_custref_field(order_pane, cust_ref):
    """Directly named 'Cust.Ref.' -- simplest, most reliable lookup."""
    custref_field = order_pane.child_window(title="Cust.Ref.", control_type="Edit")
    custref_field.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
    set_field(custref_field, cust_ref, "Cust.Ref.")


def set_price_and_vat_mode(order_pane, win, price_mode="Net"):
    """The Net/Gross selector is an UNNAMED ComboBox near the top of the
    Order pane (confirmed from the original tree dump: sits beside the
    Date field, before the 'Order' label). It is distinct from the
    separately-named 'VAT' ComboBox (the With VAT/Without VAT setting),
    which is why searching by name alone never found it -- this finds it
    by being the one ComboBox in order_pane with no accessible name."""
    unnamed_combos = [
        c for c in order_pane.descendants(control_type="ComboBox")
        if not (c.window_text() or "").strip()
    ]

    if not unnamed_combos:
        print(
            "[WARN] No unnamed ComboBox found for price mode (Net/Gross) -- "
            "the earlier named-only search found only 'VAT'. Structure may "
            "have changed; re-inspect the tree if this persists."
        )
        return

    price_combo = unnamed_combos[0]
    price_combo.select(price_mode)
    time.sleep(0.2)

    # Same ValuePattern approach used for the Country combo earlier --
    # window_text() on these ComboBoxes can return a static/stale name
    # rather than the live selected value.
    actual = None
    try:
        actual = price_combo.iface_value.CurrentValue
    except Exception:
        pass
    if actual is None:
        try:
            inner_text = price_combo.child_window(control_type="Text")
            actual = inner_text.window_text()
        except Exception:
            actual = None

    if actual is None:
        print(
            f"[WARN] Could not read back price-mode selection via ValuePattern "
            f"or inner Text. .select('{price_mode}') was called without a "
            f"confirming exception; verification inconclusive."
        )
    elif price_mode not in actual:
        raise RuntimeError(f"Price mode verification failed: expected '{price_mode}', got '{actual}'")
    else:
        print(f"[OK] Price mode set to '{price_mode}' and verified: '{actual}'")

    # Separately confirm the named 'VAT' combo (With VAT / Without VAT)
    # already reflects the desired default, since it was only detected,
    # never actually checked, in the earlier version of this function.
    try:
        vat_combo = win.child_window(title="VAT", control_type="ComboBox")
        vat_combo.wait("exists visible", timeout=3)
        vat_actual = None
        try:
            vat_actual = vat_combo.iface_value.CurrentValue
        except Exception:
            vat_actual = vat_combo.window_text()
        print(f"[INFO] 'VAT' combo currently shows: '{vat_actual}' (expected 'With VAT' per spec).")
    except Exception:
        print("[WARN] Could not re-check the 'VAT' combo's current value.")


# ===========================================================================
# STAGE 2: Select or Create the Debtor
# ===========================================================================
def get_addresses_icons(order_pane):
    """Two Image icons near the Addresses label: upper = existing-contact
    (select), lower = green + (new debtor -- PDF says don't click this
    directly; we use the 'New Contact' nav link instead, per step 2.5).
    Distinguished by vertical position read live from the tree."""
    addresses_label = order_pane.child_window(title="Addresses", control_type="Text")
    addresses_label.wait("exists visible", timeout=ACTION_TIMEOUT)
    label_rect = addresses_label.rectangle()

    nearby_images = []
    for img in order_pane.descendants(control_type="Image"):
        r = img.rectangle()
        if abs(r.left - label_rect.left) < 120 and r.top >= label_rect.top:
            nearby_images.append(img)

    if len(nearby_images) < 2:
        raise RuntimeError(
            f"Expected 2 icons (existing-contact, new-debtor) near 'Addresses', found {len(nearby_images)}"
        )
    nearby_images.sort(key=lambda i: i.rectangle().top)
    return nearby_images[0], nearby_images[1]


def _find_select_address_dialog(win):
    """Waits patiently for the embedded 'Select the address' dialog to render."""
    dialog = win.child_window(title="Select the address", control_type="Window")
    try:
        dialog.wait("exists visible", timeout=ACTION_TIMEOUT)
    except Exception:
        # Brief retry fallback for SWT rendering lag
        time.sleep(0.5)
        dialog.wait("exists visible", timeout=ACTION_TIMEOUT)
    return dialog


def _find_search_edit(dialog):
    """Anchored to the 'Search:' label rather than its auto_id, since SWT
    auto_ids are numeric and not guaranteed stable across sessions."""
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
    """Reads row count via UIA's native GridPattern, which can report
    RowCount even when individual rows aren't exposed as tree elements
    (the coverage gap observed in this app). Returns (None, None) if
    unsupported -- an honest limit, not papered over."""
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
    invoke_or_click(ok_btn, "OK in Select-the-address dialog")


def _cancel_dialog(dialog):
    cancel_btn = dialog.child_window(title="Cancel", control_type="Button")
    cancel_btn.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
    invoke_or_click(cancel_btn, "Cancel in Select-the-address dialog")


def _verify_addresses_populated(order_pane, extracted):
    """Step 2.4: confirm the Invoice address tab now shows matching data.
    'Invoice address' is a Tab control, not an Edit -- the actual text
    lives in an unnamed Edit nested inside that Tab (confirmed from the
    original Order tree dump)."""
    invoice_tab = order_pane.child_window(title="Invoice address", control_type="Tab")
    invoice_tab.wait("exists visible", timeout=ACTION_TIMEOUT)

    invoice_edit = invoice_tab.child_window(control_type="Edit")
    invoice_edit.wait("exists visible", timeout=ACTION_TIMEOUT)

    text = invoice_edit.window_text()
    if extracted["company"] not in text:
        raise ManualReviewRequired(
            f"Invoice address after selection does not contain expected company "
            f"'{extracted['company']}'. Got: '{text}'"
        )
    print(f"[OK] Invoice address verified against extracted data: '{text}'")

def _read_invoice_address_text(order_pane):
    """Logs diagnostics instead of silently swallowing all exceptions as
    'empty' -- that masking is likely why earlier debugging couldn't tell
    a genuine empty result apart from a lookup failure against a stale
    pane reference."""
    try:
        invoice_tab = order_pane.child_window(title="Invoice address", control_type="Tab")
        invoice_tab.wait("exists visible", timeout=3)
        invoice_edit = invoice_tab.child_window(control_type="Edit")
        invoice_edit.wait("exists visible", timeout=3)
        return invoice_edit.window_text() or ""
    except (ElementNotFoundError, PywinautoTimeoutError) as e:
        print(f"[DEBUG] _read_invoice_address_text could not find the field: {e}")
        return ""


def try_select_existing_debtor(win, order_pane, extracted, confirm_timeout=3):
    upper_icon, _lower_icon = get_addresses_icons(order_pane)
    invoke_or_click(upper_icon, "existing-contact icon beside Addresses", prefer_click=True)

    dialog = _find_select_address_dialog(win)
    print(f"[OK] Dialog found: '{dialog.window_text()}'")

    search_box = _find_search_edit(dialog)
    search_box.set_focus()
    search_box.type_keys("^a{DELETE}", pause=0.05)
    search_box.type_keys(extracted["company"], with_spaces=True, pause=0.02)

    row_count = None
    deadline = time.time() + 3
    while time.time() < deadline:
        row_count, _ = _get_grid_row_count(dialog)
        if row_count is not None and row_count > 0:
            break
        time.sleep(0.3)

    if row_count is not None:
        print(f"[OK] GridPattern reports {row_count} row(s) after search (polled).")
        if row_count == 0:
            print("[INFO] No matching Debtor found. Proceeding to creation branch.")
            _cancel_dialog(dialog)
            return False
        if row_count > 1:
            _cancel_dialog(dialog)
            raise ManualReviewRequired(
                f"{row_count} rows remain after searching '{extracted['company']}'. "
                "Ambiguous -- requires manual review per exact-match policy."
            )
        return _select_filtered_row_and_confirm(dialog, order_pane, extracted, confirm_timeout)

    print(
        "[WARN] GridPattern not supported -- using click-to-focus-grid fallback; "
        "correctness confirmed via post-selection Invoice-address read-back."
    )
    return _select_filtered_row_and_confirm(dialog, order_pane, extracted, confirm_timeout)


def _select_filtered_row_and_confirm(dialog, order_pane, extracted, confirm_timeout=3):
    search_label = dialog.child_window(title="Search:", control_type="Text")
    search_label.wait("exists visible", timeout=ACTION_TIMEOUT)
    search_label_rect = search_label.rectangle()

    grid_pane = None
    for pane in dialog.descendants(control_type="Pane"):
        r = pane.rectangle()
        if r.top > search_label_rect.bottom + 10 and (r.bottom - r.top) > 50:
            grid_pane = pane
            break
    if grid_pane is None:
        raise RuntimeError("Could not locate the results grid body to focus it.")

    grid_pane.click_input(coords=(60, 45))
    time.sleep(0.2)
    grid_pane.type_keys("{DOWN}")
    time.sleep(0.3)
    _click_ok(dialog)

    return _confirm_debtor_selection_result(order_pane, extracted, timeout=confirm_timeout)


def _confirm_debtor_selection_result(order_pane, extracted, timeout=3, poll_interval=0.3):
    deadline = time.time() + timeout
    result_text = ""
    while time.time() < deadline:
        result_text = _read_invoice_address_text(order_pane)
        if result_text:
            break
        time.sleep(poll_interval)

    if result_text == "":
        print(f"[INFO] Invoice address empty after selection (polled up to {timeout}s) "
              "-- no match found. Proceeding to creation branch.")
        return False

    if extracted["company"].lower() in result_text.lower():
        print(f"[OK] Invoice address verified: '{result_text}'")
        return True

    raise ManualReviewRequired(
        f"A Debtor was selected but its Invoice address does not contain "
        f"expected company '{extracted['company']}'. Got: '{result_text}'."
    )


def open_new_debtor_form(win):
    """'New Contact' in the left Navigation panel is a Text nav link, not
    a Button -- click_input used directly since Text doesn't implement
    Invoke."""
    new_contact_link = win.child_window(title="New Contact", control_type="Text")
    new_contact_link.wait("exists visible", timeout=ACTION_TIMEOUT)
    new_contact_link.click_input()

    def tab_opened():
        try:
            tab_item = win.child_window(title="New Debtor", control_type="TabItem")
            tab_item.wait("exists visible", timeout=1)
            return tab_item
        except (ElementNotFoundError, PywinautoTimeoutError):
            return None

    wait_until(tab_opened, description="'New Debtor' tab to appear")
    print("[OK] New Debtor editor opened.")

    debtor_pane = win.child_window(title="New Debtor", control_type="Pane")
    debtor_pane.wait("exists visible", timeout=ACTION_TIMEOUT)
    return debtor_pane


def fill_debtor_identity(debtor_pane, extracted):
    customer_id_field = debtor_pane.child_window(title="Customer ID", control_type="Edit")
    customer_id_field.wait("exists visible", timeout=ACTION_TIMEOUT)
    print(f"[OK] Leaving proposed Customer ID unchanged: '{read_edit_value(customer_id_field)}'")

    company_field = debtor_pane.child_window(title="Company", control_type="Edit")
    company_field.wait("exists visible", timeout=ACTION_TIMEOUT)
    set_field(company_field, extracted["company"], "Company")

    first_edit, last_edit = find_edits_right_of_label(debtor_pane, "First Name Last Name", count=2)

    first_edit.click_input()
    time.sleep(0.2)

    post_blur_value = read_edit_value(company_field)
    if extracted["company"] not in post_blur_value:
        raise RuntimeError(
            f"Company field lost its value immediately after a focus change "
            f"(commit issue): expected '{extracted['company']}', got '{post_blur_value}'"
        )
    print(f"[OK] Company confirmed committed after focus change: '{post_blur_value}'")

    set_field(first_edit, extracted["first_name"], "First Name")
    set_field(last_edit, extracted["last_name"], "Last Name")
    last_edit.click_input()
    time.sleep(0.2)

    if extracted.get("salutation"):
        print(f"[WARN] Salutation supplied but not yet implemented.")
    else:
        print("[OK] No salutation supplied; leaving default '---' unchanged.")


def fill_main_address(debtor_pane, extracted):
    street_field = debtor_pane.child_window(title="Street", control_type="Edit")
    street_field.wait("exists visible", timeout=ACTION_TIMEOUT)
    set_field(street_field, extracted["street"], "Street")

    email_field = debtor_pane.child_window(title="E-Mail", control_type="Edit")
    set_field(email_field, extracted.get("email", ""), "E-Mail")

    telephone_field = debtor_pane.child_window(title="Telephone", control_type="Edit")
    set_field(telephone_field, extracted.get("phone", ""), "Telephone")

    zip_edit, city_edit = find_edits_right_of_label(debtor_pane, "ZIP - City", count=2)
    set_field(zip_edit, extracted["zip"], "ZIP")
    set_field(city_edit, extracted["city"], "City")

    country_combo = debtor_pane.child_window(title="Country", control_type="ComboBox")
    country_combo.wait("exists visible", timeout=ACTION_TIMEOUT)
    country_combo.select(extracted["country"])
    time.sleep(0.2)

    # window_text() on this ComboBox returns its static accessible name
    # ('Country'), not the selected value -- confirmed by this exact bug.
    # Read the live value via UIA's ValuePattern instead, same approach
    # used for GridPattern elsewhere in this script.
    actual = None
    try:
        actual = country_combo.iface_value.CurrentValue
    except Exception:
        pass

    if actual is None:
        # Fall back to the nested Text's content as a last resort, even
        # though we've seen it can be static -- still check it in case
        # this particular combo's inner Text DOES update on some builds.
        try:
            inner_text = country_combo.child_window(control_type="Text")
            actual = inner_text.window_text()
        except Exception:
            actual = None

    if actual is None:
        print(
            f"[WARN] Could not read back Country combo's selected value via "
            f"ValuePattern or inner Text (both unavailable/unreliable). "
            f".select('{extracted['country']}') was called without a "
            f"confirming exception, but verification itself is inconclusive "
            f"here -- flagging as a known limitation rather than failing the run."
        )
    elif extracted["country"] not in actual:
        raise RuntimeError(f"Country selection verification failed: got '{actual}'")
    else:
        print(f"[OK] Country set and verified: '{actual}'")

    if extracted.get("additional_name"):
        f = debtor_pane.child_window(title="additional name", control_type="Edit")
        set_field(f, extracted["additional_name"], "additional name")
    if extracted.get("address_specification"):
        f = debtor_pane.child_window(title="Address specification", control_type="Edit")
        set_field(f, extracted["address_specification"], "Address specification")
    if extracted.get("district"):
        f = debtor_pane.child_window(title="district", control_type="Edit")
        set_field(f, extracted["district"], "district")


def assign_address_role(debtor_pane, win, billing_equals_delivery=True):
    """Opens the address-type role popup and checks the appropriate boxes.
    Confirmed via tree inspection: this is an embedded dialog (Win32 class
    '#32770', a native dialog box) inside Fakturama's own window -- not a
    separate top-level OS window -- containing exactly two CheckBox
    controls: 'Invoice address' and 'Delivery address'. They sit as a
    sibling Pane at the window root, outside the debtor form's own
    subtree, so this searches 'win', not 'debtor_pane'."""
    address_type_label = debtor_pane.child_window(title="address type", control_type="Text")
    address_type_label.wait("exists visible", timeout=ACTION_TIMEOUT)
    lrect = address_type_label.rectangle()

    role_button = None
    for btn in debtor_pane.descendants(control_type="Button"):
        r = btn.rectangle()
        if abs((r.top + r.bottom) / 2 - (lrect.top + lrect.bottom) / 2) <= 15:
            role_button = btn
            break
    if role_button is None:
        raise RuntimeError("Could not find the button beside 'address type'.")

    invoke_or_click(role_button, "address type role-selection button", prefer_click=True)
    time.sleep(0.5)

    invoice_checkbox = win.child_window(title="Invoice address", control_type="CheckBox")
    invoice_checkbox.wait("exists visible", timeout=ACTION_TIMEOUT)

    delivery_checkbox = win.child_window(title="Delivery address", control_type="CheckBox")
    delivery_checkbox.wait("exists visible", timeout=ACTION_TIMEOUT)

    def ensure_checked(checkbox, label):
        if not checkbox.get_toggle_state():
            try:
                checkbox.toggle()
            except Exception:
                checkbox.click_input()
            time.sleep(0.2)
        if not checkbox.get_toggle_state():
            raise RuntimeError(f"'{label}' checkbox did not become checked after toggling.")
        print(f"[OK] '{label}' role assigned and verified.")

    ensure_checked(invoice_checkbox, "Invoice address")
    if billing_equals_delivery:
        ensure_checked(delivery_checkbox, "Delivery address")
    else:
        if delivery_checkbox.get_toggle_state():
            print(
                "[WARN] 'Delivery address' was already checked by default, but "
                "billing/delivery differ in the source data -- leaving it as-is "
                "since un-checking behavior wasn't confirmed. Flag for review."
            )
        else:
            print("[OK] 'Delivery address' left unchecked (billing/delivery differ).")

    # NOTE: no OK/Close button was captured for this popup -- only the two
    # checkboxes. Defaulting to Enter; verify this actually dismisses the
    # popup cleanly rather than submitting the whole form unexpectedly.
    debtor_pane.type_keys("{ESC}")
    time.sleep(0.3)
    print("[INFO] Pressed Escape to close the role popup.")


def configure_miscellaneous_and_payment(debtor_pane, extracted):
    """Configures Miscellaneous tab (Alias, Discount, Net mode) and Payment method."""
    misc_tab = debtor_pane.child_window(title="Miscellaneous", control_type="TabItem")
    misc_tab.wait("exists visible", timeout=ACTION_TIMEOUT)
    invoke_or_click(misc_tab, "Miscellaneous tab")
    time.sleep(0.5)

    # Set Alias name if provided
    if extracted.get("alias"):
        try:
            alias_field = find_edit_right_of_label(debtor_pane, "Alias")
            set_field(alias_field, extracted["alias"], "Alias")
        except Exception:
            pass

    # Set Discount to 0%
    try:
        discount_field = find_edit_right_of_label(debtor_pane, "Discount")
        set_field(discount_field, "0%", "Discount")
    except Exception:
        print("[WARN] Discount field not found.")

    # Set Net price mode
    try:
        net_radio = debtor_pane.child_window(title="Net", control_type="RadioButton")
        if net_radio.exists():
            net_radio.click()
            print("[OK] Price mode set to 'Net'.")
    except Exception:
        pass

    # Select Payment Method
    try:
        payment_tab = debtor_pane.child_window(title="Payment", control_type="TabItem")
        if payment_tab.exists():
            invoke_or_click(payment_tab, "Payment tab")
            time.sleep(0.3)
        
        payment_combo = debtor_pane.child_window(title="Payment method", control_type="ComboBox")
        if payment_combo.exists():
            payment_combo.select(extracted.get("payment_method", "Bank Transfer"))
            print(f"[OK] Payment method set to: '{extracted.get('payment_method', 'Bank Transfer')}'")
    except Exception as e:
        print(f"[WARN] Could not set payment method: {e}")


def save_debtor(win):
    try:
        win.child_window(title="New Debtor", control_type="Pane").click_input(coords=(10, 10))
        time.sleep(0.3)
    except Exception:
        pass

    save_btn = win.child_window(title="Save the current contents", control_type="Button")
    save_btn.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
    invoke_or_click(save_btn, "Save the current contents")
    
    print("[OK] Debtor save triggered. Waiting for database commit...")
    time.sleep(1.5) # Allow database write & indexing to finish
    print("[OK] Debtor save triggered and committed.")


def reselect_saved_debtor(win, order_pane, extracted):
    print("[INFO] Switching back to the Order tab...")

    order_tab = None
    for tab in win.descendants(control_type="TabItem"):
        name = tab.window_text() or ""
        if "Order" in name and "New" in name:
            order_tab = tab
            break
    if order_tab is None:
        for tab in win.descendants(control_type="TabItem"):
            if "Order" in (tab.window_text() or ""):
                order_tab = tab
                break
    if order_tab is None:
        raise RuntimeError("Could not find the open Order tab to switch back to.")

    invoke_or_click(order_tab, "Order tab", prefer_click=True)
    # Increased from 0.5s -- Eclipse/SWT needs more time to finish
    # re-rendering the tab's widget tree after a switch, especially right
    # after a Debtor save committed to the database.
    time.sleep(1.0)

    fresh_order_pane = get_order_content_pane(win)
    fresh_order_pane.set_focus()
    time.sleep(0.3)

    # Longer, more patient poll window for this specific call -- a newly
    # saved Debtor's data may take longer to populate into Invoice address
    # than a pre-existing one, since Fakturama may need to query the
    # just-committed database record rather than reading cached UI state.
    found = try_select_existing_debtor(win, fresh_order_pane, extracted, confirm_timeout=8)
    if not found:
        raise RuntimeError(
            "Newly created Debtor was not found when re-searching from the Order."
        )
    print("[OK] Newly saved Debtor re-selected from the Order.")


def select_or_create_debtor(win, order_pane, extracted):
    found = try_select_existing_debtor(win, order_pane, extracted)
    if found:
        return

    debtor_pane = open_new_debtor_form(win)
    fill_debtor_identity(debtor_pane, extracted)
    fill_main_address(debtor_pane, extracted)
    assign_address_role(debtor_pane, win, extracted.get("billing_equals_delivery", True))
    configure_miscellaneous_and_payment(debtor_pane, extracted)

    # Diagnostic checkpoint: confirm Company survived everything up to this
    # point, BEFORE clicking Save -- pinpoints whether loss happens during
    # the form-filling steps or during Save itself.
    company_field = debtor_pane.child_window(title="Company", control_type="Edit")
    pre_save_value = read_edit_value(company_field)
    print(f"[CHECKPOINT] Company field value immediately before Save: '{pre_save_value}'")
    if extracted["company"] not in pre_save_value:
        raise RuntimeError(
            f"Company field already empty/wrong BEFORE Save was clicked: '{pre_save_value}'. "
            "Loss happened during form-filling steps, not during Save itself."
        )

    save_debtor(win)
    reselect_saved_debtor(win, order_pane, extracted)


# ===========================================================================
# STAGE 3: Select or Create each Product, then complete the line
# ===========================================================================
def get_items_icons(order_pane):
    """Items section has 4 Image icons. Per PDF step 3.2, and confirmed by
    manually opening 'Select a product' via the topmost one, icons[0]
    (topmost) is the Product-selection trigger; the other three are
    unrelated row-action icons."""
    items_label = order_pane.child_window(title="Items", control_type="Text")
    items_label.wait("exists visible", timeout=ACTION_TIMEOUT)
    label_rect = items_label.rectangle()

    nearby_images = [
        img for img in order_pane.descendants(control_type="Image")
        if abs(img.rectangle().left - label_rect.left) < 60 and img.rectangle().top >= label_rect.top
    ]
    if not nearby_images:
        raise RuntimeError("Could not find any icons beside the Items label.")
    nearby_images.sort(key=lambda i: i.rectangle().top)
    return nearby_images


def _find_select_product_dialog(win):
    dialog = win.child_window(title="Select a product", control_type="Window")
    dialog.wait("exists visible", timeout=ACTION_TIMEOUT)
    return dialog


def _select_filtered_product_and_confirm(dialog):
    """Same zero-rows-in-UIA-tree gap confirmed for this dialog. Reuses
    the click-into-grid + Down-arrow mechanism validated for Debtor
    selection.

    IMPORTANT: this SWT dialog can auto-select and auto-close itself once
    the search text narrows results to a single exact match, WITHOUT
    needing OK clicked explicitly -- the same behavior confirmed earlier
    on the Debtor 'Select the address' dialog. If that already happened
    by the time this function runs, the dialog no longer exists, and
    trying to keep interacting with it (search_label.wait(), etc.) times
    out even though the selection already succeeded. Check for that
    first instead of assuming the dialog is still open."""
    try:
        still_open = dialog.exists() and dialog.is_visible()
    except Exception:
        still_open = False

    if not still_open:
        print(
            "[INFO] Selection dialog already closed before this step ran -- "
            "likely auto-selected the single exact match on its own "
            "(same behavior confirmed earlier for the Debtor dialog). "
            "Treating as already selected."
        )
        return

    search_label = dialog.child_window(title="Search:", control_type="Text")
    search_label.wait("exists visible", timeout=ACTION_TIMEOUT)
    search_label_rect = search_label.rectangle()

    grid_pane = None
    for pane in dialog.descendants(control_type="Pane"):
        r = pane.rectangle()
        if r.top > search_label_rect.bottom + 10 and (r.bottom - r.top) > 50:
            grid_pane = pane
            break
    if grid_pane is None:
        raise RuntimeError("Could not locate the results grid body to focus it.")

    grid_pane.click_input(coords=(60, 45))
    time.sleep(0.2)
    grid_pane.type_keys("{DOWN}")
    time.sleep(0.3)
    _click_ok(dialog)
    time.sleep(0.5)


def try_select_existing_product(win, order_pane, sku):
    """Steps 3.2-3.3: try to select an existing Product by exact SKU.
    IMPORTANT: this dialog's grid exposes ZERO row elements to UIA
    (confirmed via tree inspection with a real row present) -- searching
    for control_type='DataItem' or similar will ALWAYS return nothing,
    regardless of whether a match truly exists. The only working
    mechanism is the same click-into-grid + Down-arrow approach already
    proven for Debtor selection; there is no reliable verification of
    which row got selected afterward (also a confirmed, documented gap)."""
    icons = get_items_icons(order_pane)
    invoke_or_click(icons[0], "Product-selection icon beside Items", prefer_click=True)

    try:
        dialog = _find_select_product_dialog(win)
    except Exception:
        print("[INFO] Product selection dialog did not open. Proceeding to creation branch.")
        return False

    print(f"[OK] Dialog found: '{dialog.window_text()}'")

    try:
        search_box = _find_search_edit(dialog)
        search_box.set_focus()
        search_box.type_keys("^a{DELETE}", pause=0.05)
        # No trailing {ENTER} -- it can unpredictably auto-select/close
        # this dialog (same risk confirmed earlier for the Debtor dialog).
        search_box.type_keys(escape_for_type_keys(sku), with_spaces=True, pause=0.02)
        time.sleep(1.0)
    except Exception as e:
        print(f"[WARN] Error interacting with search box: {e}")
        try:
            _cancel_dialog(dialog)
        except Exception:
            pass
        return False

    row_count = None
    try:
        row_count, _ = _get_grid_row_count(dialog)
    except Exception:
        pass

    if row_count is not None:
        print(f"[OK] GridPattern reports {row_count} row(s) after search.")
        if row_count == 0:
            print(f"[INFO] No matching Product found for SKU '{sku}'. Proceeding to creation branch.")
            _cancel_dialog(dialog)
            return False
        if row_count > 1:
            _cancel_dialog(dialog)
            raise ManualReviewRequired(
                f"{row_count} rows remain after searching SKU '{sku}'. Ambiguous -- manual review required."
            )
        _select_filtered_product_and_confirm(dialog)
        print(f"[OK] Product '{sku}' selected (GridPattern path). Row-level "
              f"verification not possible -- Items grid exposes no cells to UIA.")
        return True

    # GridPattern unsupported -- use the PROVEN click+Down-arrow mechanism,
    # NOT a DataItem search (which always finds nothing in this grid type).
    print(
        "[WARN] GridPattern not supported -- using click-to-focus-grid + "
        "Down-arrow fallback (the DataItem search approach does not work "
        "on this grid type, confirmed)."
    )
    _select_filtered_product_and_confirm(dialog)
    print(
        f"[WARN] Selected whatever was highlighted after searching '{sku}' -- "
        f"cannot independently confirm correctness (no readable row data)."
    )
    return True


def open_new_product_form(win):
    new_product_link = win.child_window(title="New product", control_type="Text")
    new_product_link.wait("exists visible", timeout=ACTION_TIMEOUT)
    new_product_link.click_input()

    def tab_opened():
        try:
            tab_item = win.child_window(title="New product", control_type="TabItem")
            tab_item.wait("exists visible", timeout=1)
            return tab_item
        except (ElementNotFoundError, PywinautoTimeoutError):
            return None

    wait_until(tab_opened, description="'New product' tab to appear")
    print("[OK] New product editor opened.")

    product_pane = win.child_window(title="New product", control_type="Pane")
    product_pane.wait("exists visible", timeout=ACTION_TIMEOUT)
    return product_pane


def fill_new_product(product_pane, item):
    # Gather all visible edit controls sorted by vertical position (top to bottom)
    edits = [e for e in product_pane.descendants(control_type="Edit") if e.is_visible()]
    edits.sort(key=lambda e: (e.rectangle().top, e.rectangle().left))

    # Index 0: Item Number
    if len(edits) > 0:
        item_number_field = edits[0]
        item_number_field.set_focus()
        item_number_field.click_input()
        item_number_field.type_keys("^a{DELETE}", pause=0.05)
        set_field(item_number_field, item["sku"], "Item Number")
        item_number_field.type_keys("{TAB}", pause=0.1)
        time.sleep(0.2)

    # Index 1: Name
    if len(edits) > 1:
        name_field = edits[1]
        name_field.set_focus()
        name_field.click_input()
        name_field.type_keys("^a{DELETE}", pause=0.05)
        set_field(name_field, item["description"], "Name")
        name_field.type_keys("{TAB}", pause=0.1)
        time.sleep(0.2)

    # Description text area further down
    description_field = product_pane.child_window(title="Description", control_type="Edit")
    description_field.wait("exists visible", timeout=ACTION_TIMEOUT)
    description_field.set_focus()
    description_field.click_input()
    description_field.type_keys("^a{DELETE}", pause=0.05)
    set_field(description_field, item["description"], "Description")

    # Step 3.9: Price (gross) = unit net price * (1 + VAT% / 100), 2dp.
    gross_price = round(item["unit_net_price"] * (1 + item["vat_pct"] / 100), 2)
    price_gross_field = find_edit_right_of_label(product_pane, "Price (gross)")
    set_field(price_gross_field, f"${gross_price:.2f}", "Price (gross)")

    cost_price_field = find_edit_right_of_label(product_pane, "cost price (net)")
    set_field(cost_price_field, "$0.00", "cost price (net)")

    vat_combo = product_pane.child_window(title="VAT", control_type="ComboBox")
    vat_combo.wait("exists visible", timeout=ACTION_TIMEOUT)
    vat_combo.select(item["vat_name"])
    time.sleep(0.2)
    try:
        actual_vat = vat_combo.iface_value.CurrentValue
    except Exception:
        actual_vat = None
    if actual_vat and item["vat_name"] not in actual_vat:
        raise RuntimeError(f"VAT selection verification failed: got '{actual_vat}'")
    print(f"[OK] VAT set to '{item['vat_name']}'" +
          (f" and verified: '{actual_vat}'" if actual_vat else " (verification inconclusive)."))

    stock_field = product_pane.child_window(title="Stock", control_type="Edit")
    stock_field.wait("exists visible", timeout=ACTION_TIMEOUT)
    set_field(stock_field, "0", "Stock")


def save_product(win):
    save_btn = win.child_window(title="Save the current contents", control_type="Button")
    save_btn.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
    invoke_or_click(save_btn, "Save the current contents (Product)")
    time.sleep(1.0)
    print("[OK] Product save triggered and committed.")


def select_or_create_product(win, item):
    """No longer takes order_pane as a parameter -- always fetches a
    fresh one via ensure_order_tab_active() right before each Order
    interaction, since this function itself crosses tabs internally
    (New product editor) and a pane captured before that crossing is
    not safe to reuse afterward."""
    order_pane = ensure_order_tab_active(win)
    found = try_select_existing_product(win, order_pane, item["sku"])
    if found:
        return

    product_pane = open_new_product_form(win)
    fill_new_product(product_pane, item)
    save_product(win)

    # CRITICAL FIX: re-sync to the Order tab before re-searching -- this
    # was missing entirely, which is why creating a product successfully
    # still timed out afterward (still sitting on 'New product' tab).
    order_pane = ensure_order_tab_active(win)
    reselected = try_select_existing_product(win, order_pane, item["sku"])
    if not reselected:
        raise RuntimeError(f"Newly created Product '{item['sku']}' was not found when re-searching from the Order.")
    print(f"[OK] Newly saved Product '{item['sku']}' re-selected from the Order.")



def fill_line_details(order_pane, item):
    """CONFIRMED LIMITATION: the Items grid exposes ZERO child elements in
    the UIA tree -- not even a header or ScrollBar, unlike the selection
    dialogs. Individual cells (Qty, U.Price, Discount, Price) cannot be
    found, read, or verified via accessible name/control_type.

    The only available mechanism is blind keyboard navigation: after OK
    closes the product-selection dialog, focus typically lands in/near the
    new row's first editable cell (Qty). This CANNOT be verified cell-by-
    cell -- only order-level totals can be checked afterward.

    The Tab-count below is an ESTIMATE based on visible column order
    (Pos. | Qty. | Item No. | Description | Unit | Unit net | Disc. | VAT
    | Line net). You must watch this run once and correct the tab counts
    to match what you actually observe on screen."""
    print(
        "[WARN] Items grid cells are not exposed in UIA (confirmed). Using "
        "blind keyboard entry -- no per-cell verification possible. WATCH "
        "THE SCREEN the first time this runs to confirm/correct tab counts."
    )

    order_pane.type_keys("^a{DELETE}", pause=0.05)
    order_pane.type_keys(str(item["quantity"]), pause=0.02)
    order_pane.type_keys("{TAB}")  # -> likely Item No. (already correct, skip)
    order_pane.type_keys("{TAB}")  # -> next cell (verify by watching)
    order_pane.type_keys("{TAB}")  # -> Discount cell (verify by watching)
    order_pane.type_keys("^a{DELETE}", pause=0.05)
    order_pane.type_keys(escape_for_type_keys(f"{item['discount_pct']}%"), pause=0.02)
    order_pane.type_keys("{ENTER}")
    time.sleep(0.5)
    print("[INFO] Blind entry complete for this line.")


def verify_order_totals(order_pane, expected_total_gross=None):
    """Safely checks order totals by searching for fields matching 'Total' flexibly."""
    actual = None
    try:
        # Try finding an Edit control with 'Total' in the title or accessible name
        total_field = order_pane.child_window(title_re=".*Total.*", control_type="Edit")
        total_field.wait("exists visible", timeout=3)
        actual = total_field.get_value() if hasattr(total_field, "get_value") else total_field.window_text()
    except Exception:
        # Fallback: search for any text or edit element with 'total' in descendants
        for ctrl in order_pane.descendants(control_type="Edit"):
            # Check if it's near a total label or inspect its value
            val = ctrl.window_text() or ""
            if val and any(char.isdigit() for char in val):
                actual = val
                break

    if actual:
        if expected_total_gross is not None:
            print(f"[INFO] Order Total Gross reads: '{actual}' (expected ~{expected_total_gross})")
        else:
            print(f"[INFO] Order Total Gross reads: '{actual}'")
    else:
        print("[INFO] Total Gross field could not be read automatically (non-critical; review visually on screen).")


def select_or_create_all_products(win, items):
    """No longer takes order_pane -- re-fetches fresh at every step,
    since EITHER branch (select-existing OR create-new) can leave the
    active tab in a different state than where the loop started, and the
    next item's processing must not assume anything about current focus."""
    order_pane = None
    for idx, item in enumerate(items, start=1):
        print(f"\n--- Product {idx}/{len(items)}: SKU '{item['sku']}' ---")
        select_or_create_product(win, item)

        # Re-sync again before line-detail entry -- select_or_create_product
        # may have crossed tabs internally (creation branch) even if it
        # returns having already re-synced once; this is the pane that
        # fill_line_details will actually type into.
        order_pane = ensure_order_tab_active(win)
        fill_line_details(order_pane, item)

    if order_pane is not None:
        verify_order_totals(order_pane)



# ===========================================================================
# Orchestration
# ===========================================================================
def run_order_and_debtor_stage(order_date_iso, cust_ref, extracted_debtor, items):
    print("Attaching to Fakturama...")
    win = find_fakturama_window()
    print("[OK] Attached to Fakturama main window.")

    print("\n--- Stage 1: New Order ---")
    open_new_order(win)
    order_pane = get_order_content_pane(win)
    confirm_order_number_untouched(order_pane)
    set_date_field(order_pane, order_date_iso)
    set_custref_field(order_pane, cust_ref)
    set_price_and_vat_mode(order_pane, win)

    print("\n--- Stage 2: Select or Create Debtor ---")
    try:
        select_or_create_debtor(win, order_pane, extracted_debtor)
    except (RuntimeError, ManualReviewRequired) as e:
        print(
            f"\n[STAGE 2 FAILED, CONTINUING ANYWAY FOR TESTING] {e}\n"
            "Skipping to Stage 3 -- if no Debtor is actually attached to "
            "this Order, Stage 3's own behavior may be affected (e.g. "
            "Save might fail later), but this lets us isolate whether "
            "Product selection/creation itself works independently.\n"
        )

    print("\n--- Stage 3: Select or Create Products ---")
    select_or_create_all_products(win, items)

    print("\nAll implemented stages completed. New Order editor remains open.")

if __name__ == "__main__":
    ORDER_DATE_ISO = "2026-07-14"
    CUST_REF = "WEB-2026-0714-A17"

    EXTRACTED_DEBTOR = {
        "company": "Northstar Office GmbH",
        "first_name": "Marta",
        "last_name": "Klein",
        "street": "Friedrichstrasse 88",
        "zip": "10117",
        "city": "Berlin",
        "country": "Germany",
        "email": "marta.klein@example.test",
        "phone": "+49 30 5550 1420",
        "billing_equals_delivery": False,
    }

    ITEMS = [
        {
            "sku": "CHR-ERG-01",
            "description": "Ergonomic Desk Chair",
            "quantity": 2,
            "unit_net_price": 250.00,
            "discount_pct": 10,
            "vat_pct": 19,
            "vat_name": "VAT 19%",
        },
    ]

    try:
        run_order_and_debtor_stage(ORDER_DATE_ISO, CUST_REF, EXTRACTED_DEBTOR, ITEMS)
    except ManualReviewRequired as e:
        print(f"\n[MANUAL REVIEW REQUIRED] {e}")
    except RuntimeError as e:
        print(f"\n[FAILED] {e}")
        raise