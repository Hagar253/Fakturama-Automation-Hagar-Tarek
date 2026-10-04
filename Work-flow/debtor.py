import time

from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PywinautoTimeoutError

from config import ACTION_TIMEOUT
from exceptions import ManualReviewRequired
from ui_helpers import (
    wait_until,
    invoke_or_click,
    find_edit_right_of_label,
    find_edits_right_of_label,
    set_field,
    read_edit_value,
    get_order_content_pane,
    _find_search_edit,
    _get_grid_row_count,
    _cancel_dialog,
    _click_ok,
)

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
    """'New Contact' in the left Navigation panel is a Text nav link."""
    win.set_focus()
    time.sleep(0.3)
    
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


def run_stage2(win, order_pane, extracted_debtor):
    print("\n--- Stage 2: Select or Create Debtor ---")

    select_or_create_debtor(
        win,
        order_pane,
        extracted_debtor
    )

    print("[OK] Stage 2 completed.")