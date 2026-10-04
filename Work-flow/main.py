from ui_helpers import find_fakturama_window
from create_order import run_stage1
from debtor import run_stage2
from products import run_stage3
from save_order import run_stage4
from exceptions import ManualReviewRequired


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
    {
        "sku": "MAT-DESK-02",
        "description": "Anti-Fatigue Mat",
        "quantity": 3,
        "unit": "pcs",
        "unit_net_price": 40.00,
        "discount": "0%",
        "vat_percentage": "19%",
        "source_total": 120.00
      }
]


def main():
    print("Attaching to Fakturama...")

    win = find_fakturama_window()

    print("[OK] Attached to Fakturama main window.")

    # -------------------------
    # Stage 1
    # -------------------------
    order_pane = run_stage1(
        win,
        ORDER_DATE_ISO,
        CUST_REF
    )

    # -------------------------
    # Stage 2
    # -------------------------
    print("\n--- Stage 2: Select or Create Debtor ---")
    try:
        run_stage2(
            win,
            order_pane,
            EXTRACTED_DEBTOR
        )
    except (RuntimeError, ManualReviewRequired) as e:
        print(
            f"\n[STAGE 2 FAILED, CONTINUING ANYWAY FOR TESTING] {e}\n"
            "Skipping to Stage 3 -- if no Debtor is actually attached to "
            "this Order, Stage 3's own behavior may be affected (e.g. "
            "Save might fail later), but this lets us isolate whether "
            "Product selection/creation itself works independently.\n"
        )

    # -------------------------
    # Stage 3
    # -------------------------
    
    try:
        run_stage3(
        win,
        ITEMS
        )
    except (RuntimeError, ManualReviewRequired) as e:
        print(
            f"\n[STAGE 3 FAILED] {e}\n"
            "Stage 3 failed, so we will continue anyway for testing purposes.\n"
        )
    
    # -------------------------
    # Stage 4
    # -------------------------
    run_stage4(win)


    print("\n================================")
    print("Workflow completed successfully.")
    print("================================")


if __name__ == "__main__":
    try:
        main()

    except ManualReviewRequired as e:
        print(
            f"\n[MANUAL REVIEW REQUIRED]\n{e}"
        )

    except RuntimeError as e:
        print(
            f"\n[FAILED]\n{e}"
        )
        raise