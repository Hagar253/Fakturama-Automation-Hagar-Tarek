import time
from datetime import datetime

from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError

from config import ACTION_TIMEOUT
from ui_helpers import (
    wait_until,
    invoke_or_click,
    find_edit_right_of_label,
    set_field,
    get_order_content_pane,
)


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


def run_stage1(win, order_date_iso, cust_ref):
    print("\n--- Stage 1: New Order ---")

    open_new_order(win)

    order_pane = get_order_content_pane(win)

    confirm_order_number_untouched(order_pane)

    set_date_field(
        order_pane,
        order_date_iso
    )

    set_custref_field(
        order_pane,
        cust_ref
    )

    return order_pane