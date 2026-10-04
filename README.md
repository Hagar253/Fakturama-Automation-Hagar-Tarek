# Fakturama Order Automation

An automated workflow script designed to streamline **order creation, debtor management, product matching/creation, and invoice generation** inside [Fakturama](https://www.fakturama.info/).

Built using **Python** and [`pywinauto`](https://pywinauto.readthedocs.io/) to navigate Fakturama's SWT-based Java user interface.

---

## Setup Instructions

### Prerequisites

* **Python 3.10+** installed on Windows
* **Fakturama** running locally with a test database configured

### Dependencies

Install the required Python packages using `pip`:

```bash
pip install pywinauto pillow
```

## Project Structure

```text
Fakturama-Automation-Hagar-Tarek/
│
├── input_image.png
├── README.md
│
├── Demo/
│   └── Fakturama Demo.mp4
│
├── docs/
│   ├── Fakturama Design Document-Hagar Tarek.docx
│   └── Fakturama Design Document-Hagar Tarek.pdf
│
├── Order extraction/
│   ├── extract_order.py
│   └── order.json
│
├── Trees/
│   ├── address_type_popup_tree.txt
│   ├── dump_tree.py
│   ├── fakturama_debtor_tree.txt
│   ├── items_row_tree.txt
│   ├── items_select_product_tree.txt
│   ├── main_page.txt
│   └── new_product_tree.txt
│
└── Work-flow/
    ├── config.py
    ├── create_order.py
    ├── debtor.py
    ├── exceptions.py
    ├── main.py
    ├── products.py
    ├── save_order.py
    └── ui_helpers.py
```
---

## How to Run

1. Launch **Fakturama** and make sure the main window is open.

2. Open a terminal in the project directory:

   ```text
   Work-flow/
   ```

3. Run the orchestration script:

   ```bash
   python main.py
   ```

---

## Trade-offs & Skipped Parts

I skipped automating **Payment Methods**, **custom VATs**, and **Miscellaneous details**, opting instead to seed them manually for this run.

To be completely honest, these fields follow the exact same form-filling logic as the debtor and product screens I already built. Writing out the code for them would have been repetitive.

Rather than spending time on duplicate form fields, I wanted to prioritize building out the **core architecture**, including:

* Handling complex multi-tab transitions
* UI tree traversal fallbacks
* Multi-document chaining
* Debtor and product matching/creation
* Automated order and invoice workflows

### How I'd Finish Them

I'd use the exact same **dialog-tree inspection pattern** already established in the debtor and product modules.

The automation would query the dialog's descendants for their control names and dynamically inject the required data without relying on manual interaction.

---

## If I Had 3 More Hours...

### AI-Powered Extraction

- Raw OCR can be unreliable when dealing with messy fonts, low-resolution images, or inconsistent layouts.

- If I had more time, I'd integrate an ** LLM** to handle the data extraction directly. This would make the extraction process much more resilient than relying solely on traditional OCR.

### Packaging

- Right now, the automation is launched directly from the terminal.

- Packaging it as a simple **local tool or executable** would make it much more practical for day-to-day use.

### Speeding Up Lookups

- Navigating complex SWT window hierarchies introduces some latency.

- Caching selectors and streamlining element searches could significantly improve the speed of the overall pipeline.
