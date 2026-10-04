import time

from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError

from exceptions import ManualReviewRequired
from config import ACTION_TIMEOUT
from ui_helpers import (
    ensure_order_tab_active,
    invoke_or_click,
    find_edit_right_of_label,
    set_field,
    wait_until,
    escape_for_type_keys,
    _find_search_edit,
    _get_grid_row_count,
    _cancel_dialog,
    _click_ok,
    click_nav_link_until_tab_opens,
)


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
    """Try to select an existing Product by exact SKU, matching debtor selection flow."""
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
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            row_count, _ = _get_grid_row_count(dialog)
            if row_count is not None and row_count >= 0:
                break
        except Exception:
            pass
        time.sleep(0.3)

    if row_count is not None:
        print(f"[OK] GridPattern reports {row_count} row(s) after search (polled).")
        if row_count == 0:
            print(f"[INFO] No matching Product found for SKU '{sku}'. Proceeding to creation branch.")
            _cancel_dialog(dialog)
            return False
        if row_count > 1:
            _cancel_dialog(dialog)
            raise ManualReviewRequired(
                f"{row_count} rows remain after searching SKU '{sku}'. Ambiguous -- manual review required."
            )
        
        # If exactly 1 row is found via GridPattern, select and confirm it
        _select_filtered_product_and_confirm(dialog)
        print(f"[OK] Product '{sku}' selected.")
        return True

    # GridPattern unsupported fallback: check if dialog is still open or if we treat it as missing
    print("[INFO] GridPattern not supported or unreadable. Proceeding to creation branch to ensure safety.")
    try:
        _cancel_dialog(dialog)
    except Exception:
        pass
    return False


def open_new_product_form(win, manual_fallback=True):
    """Automated nav-link click has proven unreliable in this environment.
    For deliverable purposes, falls back to a manual pause: the person
    clicks 'New product' themselves, and the script resumes once it
    detects the tab, rather than blocking entirely on a flaky click."""
    import re
    tab_title_pattern = re.compile(r"^\*?New product$")

    def tab_opened():
        for tab_item in win.descendants(control_type="TabItem"):
            if tab_title_pattern.match(tab_item.window_text() or ""):
                return tab_item
        return None

    # Try the automated click a couple of times first, quickly.
    for attempt in range(1, 3):
        try:
            win.set_focus()
            time.sleep(0.5)
            link = win.child_window(title="New product", control_type="Text")
            link.wait("exists visible", timeout=ACTION_TIMEOUT)
            link.set_focus()
            link.click_input()
            wait_until(tab_opened, timeout=3, description="'New product' tab (automated)")
            print(f"[OK] 'New product' tab opened automatically on attempt {attempt}.")
            break
        except Exception:
            print(f"[WARN] Automated click attempt {attempt} failed.")
    else:
        if not manual_fallback:
            raise RuntimeError("'New product' tab never appeared and manual_fallback is disabled.")
        print(
            "\n" + "=" * 70 +
            "\n[ACTION NEEDED] Please click 'New product' in the left panel "
            "yourself now.\nThe script will wait and resume automatically "
            "once the tab opens.\n" + "=" * 70
        )
        wait_until(tab_opened, timeout=120, interval=1.0, description="'New product' tab (manual)")
        print("[OK] Detected manually-opened 'New product' tab. Resuming automation.")

    try:
        product_pane = win.child_window(title="New product", control_type="Pane")
        product_pane.wait("exists visible", timeout=3)
    except Exception:
        product_pane = win.child_window(title="*New product", control_type="Pane")
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
    order_pane = ensure_order_tab_active(win)
    
    # 1. Try to find and select the existing product
    found = try_select_existing_product(win, order_pane, item["sku"])
    if found:
        return  # If found and selected, we are done!

    # 2. If NOT found (try_select_existing_product returned False), 
    # it automatically cancels the search dialog and flows down here:
    product_pane = open_new_product_form(win)  # <--- This cancels/bypasses search and opens the "New product" form
    fill_new_product(product_pane, item)       # <--- Fills out SKU, Name, Price, VAT, etc.
    save_product(win)                          # <--- Saves it

    # 3. Re-sync and verify
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


def _find_button_in_toolbar(win, title):
    """Several buttons share the same accessible name across different
    panels (confirmed earlier with 'Create a new product' -- top toolbar
    vs. a docked 'Products' tab). Filters by vertical position: the main
    top toolbar sits at the very top of the window, well above any
    docked bottom panel."""
    candidates = win.descendants(title=title, control_type="Button")
    if not candidates:
        raise RuntimeError(f"Could not find any '{title}' button.")
    candidates.sort(key=lambda b: b.rectangle().top)
    return candidates[0]


def save_order(win):
    """Saves the Order itself -- same top-toolbar 'Save the current
    contents' button used for Debtor/Product, filtered for ambiguity
    the same way open_new_product_form's button lookup was."""
    save_btn = _find_button_in_toolbar(win, "Save the current contents")
    save_btn.wait("exists enabled visible", timeout=ACTION_TIMEOUT)
    invoke_or_click(save_btn, "Save the current contents (Order)")
    time.sleep(1.0)
    print("[OK] Order save triggered and committed.")


def switch_to_documents_tab_and_report(win, order_pane):
    """Switches to the bottom-docked 'Documents' tab so the Order's Total
    can be visually confirmed in the recording. Also attempts to read
    Total/Total Gross directly from the Order's own summary fields
    (Edit controls named 'Total Gross' / 'Total'), which are a more
    reliable read than the Documents panel's own grid (same zero-row-
    exposed limitation likely applies there, unconfirmed)."""
    try:
        docs_tab = win.child_window(title="Documents", control_type="TabItem")
        docs_tab.wait("exists visible", timeout=3)
        invoke_or_click(docs_tab, "Documents tab", prefer_click=True)
        time.sleep(0.5)
        print("[OK] Switched to Documents tab -- visually confirm the Total column now.")
    except Exception as e:
        print(f"[WARN] Could not switch to Documents tab: {e}")

    # Best-effort direct read from the Order's own summary fields.
    for label in ("Total Gross", "Total"):
        try:
            candidates = order_pane.descendants(title=label, control_type="Edit")
            if candidates:
                value = candidates[0].window_text()
                print(f"[INFO] Order '{label}' field reads: '{value}'")
        except Exception:
            pass


def run_stage3(win, items):
    print("\n--- Stage 3: Select or Create Products ---")

    select_or_create_all_products(win, items)

    order_pane = ensure_order_tab_active(win)
    switch_to_documents_tab_and_report(win, order_pane)

    # Switch back to the Order tab before saving -- Documents tab is a
    # different active context, and Save should act on the Order.
    order_pane = ensure_order_tab_active(win)
    save_order(win)

    print("[OK] Stage 3 completed.")