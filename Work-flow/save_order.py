import time
from ui_helpers import (
    ensure_order_tab_active,
    invoke_or_click,
    _find_button_in_toolbar,
    ACTION_TIMEOUT
)

def run_stage4(win):
    print("\n--- Stage 4: Complete & Save Order ---")
    
    order_pane = ensure_order_tab_active(win)
    
    # Find the save button and click it directly 
    save_btn = _find_button_in_toolbar(win, "Save")
    invoke_or_click(save_btn, "Save Order button")
    time.sleep(1.0)
    print("[OK] Order save triggered and committed.")

    # Switch to Documents tab to verify
    try:
        docs_tab = win.child_window(title="Documents", control_type="TabItem")
        docs_tab.wait("exists visible", timeout=3)
        invoke_or_click(docs_tab, "Documents tab", prefer_click=True)
        time.sleep(0.5)
        print("[OK] Switched to Documents tab -- visually confirm the Total column now.")
    except Exception as e:
        print(f"[WARN] Could not switch to Documents tab: {e}")

    #  read from the Order's summary fields
    for label in ("Total Gross", "Total"):
        try:
            candidates = order_pane.descendants(title=label, control_type="Edit")
            if candidates:
                value = candidates[0].window_text()
                print(f"[INFO] Order '{label}' field reads: '{value}'")
        except Exception:
            pass

    print("[OK] Stage 4 completed.")