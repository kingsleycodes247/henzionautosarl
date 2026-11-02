# HenZion Auto SARL: Inventory, Sales & Debts System

Django + PostgreSQL (SQLite for quick local testing) + Tailwind + vanilla JavaScript.

## What's inside

| Area | What it does |
|---|---|
| **Sales (POS)** | Fast cart screen. Four payment methods: **Cash, MTN Mobile Money, Orange Money, Credit**. Mobile-money reference is stored for reconciliation. Double-click safe. **One sale can be paid several ways**, e.g. 10 000 = 5 000 cash + 3 000 Orange Money + 2 000 credit. The payments must add up to the total; the credit part alone goes to the debts register. |
| **Invoice** | Printable A4 invoice matching HenZion's template (logo, NIU `M052217397952Y`, phones `+237 673 441 221 / +237 679 910 139`, 8-row table, discount column, subtotal/VAT/total, signatures). Use *Print / Save as PDF*. |
| **Credit & debts register** | When any part of a sale is on credit, the screen asks for customer name, phone, address (+ WhatsApp, ID, guarantor) and a due date, and creates a debt for that part only. Every later payment (cash / MTN / Orange) lowers the balance until it reaches 0 and the debt shows **Paid**. Printable payment receipts. |
| **Boss controls on debts** | The boss has full **create / read / update / delete** on customers, debts and payments, plus a **Debts panel on his dashboard** (total owed, overdue, biggest debtors, overdue list with quick actions, latest payments). Every change is written to the **Audit log**. Cashiers can view debts and record payments only. |
| **Price-reduction approval** | A cashier lowers a price → the boss gets a **WhatsApp message and/or email** with an Approve/Decline link (plus the in-app *Price approvals* page and dashboard). The sale cannot be completed until he answers. **Declined or no answer = original price stays.** The server enforces this; the browser cannot bypass it. |
| **Inventory** | Products, categories, opening stock, receive stock, adjustments, low-stock alerts. Stock is an append-only ledger (every in/out is a row). |
| **Stock worth (boss only)** | What the goods on the shelves are worth **at cost price** and **at selling price**, plus potential profit, per product and per category. Shown on the boss's dashboard and the Products page. Cost prices are hidden from cashiers. |
| **Reports** | Daily / weekly / monthly: sales by method, money received, credit issued vs collected, top products, profit, discounts, overdue debts, low stock. In-app page + **emailed HTML report with CSV attachment**. |
| **Items from neighbors** | When a customer needs something the shop doesn't stock, the cashier taps **+ Item from a neighbor** on the sale screen, types the item, the neighbor, what the neighbor charges and the price to the customer. It appears on the customer's invoice like any item, but is **not added to products or stock and not counted in sales totals**. Only its **profit (or loss)** is added to the boss's profit. The boss has a **Neighbor items** table (profit/loss per line, filters, who is owed what, *Mark paid* / *Settle*, edit the neighbor's price) and a profit panel on his dashboard. The neighbor's name and cost never print on the customer's invoice and cashiers never see profit. |
| **Users** | Roles: **Owner/Boss** and **Cashier**. Audit log. |

## Quick start (about 3 minutes)

Requires Python 3.10+.

**macOS / Linux**
```bash
cd henzion
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py makemigrations core inventory sales debts reports
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

**Windows (PowerShell)**
```powershell
cd henzion
python -m venv venv ; venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python manage.py makemigrations core inventory sales debts reports
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Open http://localhost:8000

| Role | Username | Password |
|---|---|---|
| Boss | `boss` | `boss12345` |
| Cashier | `cashier` | `cashier12345` |

> Change these passwords (Users page) before real use. `seed_demo` also puts a placeholder boss email/WhatsApp in **Settings**; replace them with the real ones.

The pages load Tailwind from its CDN, so the first run needs internet. For a production/offline build see "Production notes".

### Run the automated tests
```bash
python manage.py test
```
Covers stock deduction, all four payment methods, VAT, idempotent sales, discount approval rules (pending/declined/expired/one-time use/tampered link), credit sales, debt balances, boss CRUD vs cashier restrictions, dashboards, reports and email sending, split payments and items fetched from neighbors.

## Try the main flows

1. **Cash / Mobile Money sale:** log in as `cashier` → *New sale* → tap products → choose a method → *Complete sale* → *Print invoice*.
2. **Split / credit sale:** put a price total of 10 000 in the cart, then type `5000` in Cash, `3000` in Orange Money and `2000` in Credit (or use "Put the rest here"). The customer + due date fields appear; the sale then shows 2 000 FCFA in the **Debts register**. Open it → *Record payment* as many times as needed; the balance drops and the status changes to *Paid* when settled.
3. **Price approval:** as `cashier`, change a unit price to something lower. The line shows *waiting for boss*. Look at the terminal running `runserver`: the WhatsApp/email message is printed there with an **Approve/Decline link** (until you configure real sending). Open the link, press *Approve*, and the cashier's screen updates within 3 seconds. Or approve it as `boss` from *Price approvals* / the dashboard. Declining (or waiting 10 minutes) restores the original price.
4. **Boss CRUD on debts:** as `boss`, open the dashboard's *Debts register* panel → *Record debt*, *Edit*, *Delete*, or open a debt to edit/delete individual payments.
5. **Reports:** as `boss` → *Reports* → *Email this report now* (printed in the terminal until SMTP is configured).

5c. **Neighbor item:** as `cashier`, on *New sale* tap **+ Item from a neighbor**, e.g. "Brake disc", neighbor "Mama Ngu", neighbor's price 1 200, we sell at 2 000, add it (optionally with normal products) and complete the sale. The invoice shows the disc; stock is unchanged. As `boss`, the dashboard's *Sales* excludes the 2 000, the *Profit & neighbor items* panel shows +800, and **Neighbor items** lists it with "Owed 1 200" until you press *Mark paid*. Selling below the neighbor's price shows in red and counts as a loss.

5b. **Stock worth:** as `boss`, open **Stock worth** in the menu (or the card on the dashboard).

> **Upgrading from the first version?** This update adds a `SalePayment` table. Run `python manage.py makemigrations` and `migrate` again. Sales made with the first version have no payment lines and won't show in the per-method totals, so if you were only testing, delete `db.sqlite3` and the `migrations/0*.py` files in each app and re-run the Quick start.

## Configuration (.env)

* **Database:** leave `DB_NAME` empty for SQLite. For PostgreSQL create a database and fill `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, then re-run `migrate`.
* **Email:** set `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (Brevo, Amazon SES, Mailgun, Gmail app password…). Until then emails print in the terminal.
* **WhatsApp (Meta Cloud API):** set `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_ID`. Meta only allows free-form messages to someone who messaged your business number in the last 24 hours, so for reliable delivery create an approved message template with **one body variable `{{1}}`** and set `WHATSAPP_TEMPLATE_NAME`. The whole request text is sent in that variable. Email is sent in parallel as a fallback.
* **`SITE_URL`:** the address used in Approve/Decline links. The boss opens them on his phone, so use the server's public address (or an ngrok URL while testing), not `localhost`.
* **Company settings (in the app → Settings):** boss email + WhatsApp number (`237679910139` format), VAT rate (0 by default; Cameroon's standard rate is 19.25), auto-approve threshold, approval timeout.

## Scheduled email reports

Reports cover **yesterday / last Mon–Sun week / last calendar month**, and each period is emailed **once** (a log prevents duplicates).

```cron
# Daily at 07:00, weekly on Monday 07:05, monthly on the 1st at 07:10 (server time)
0 7  * * *  cd /path/to/henzion && venv/bin/python manage.py send_reports daily
5 7  * * 1  cd /path/to/henzion && venv/bin/python manage.py send_reports weekly
10 7 1 * *  cd /path/to/henzion && venv/bin/python manage.py send_reports monthly
```
Test now: `python manage.py send_reports daily --force`. On Windows use Task Scheduler with the same command.

## Project layout

```
config/       settings, urls
core/         users & roles, company settings, audit log, dashboard, notifications (email/WhatsApp)
inventory/    products, categories, stock ledger
sales/        POS, sales, invoice, discount approval (services.py holds every money rule)
debts/        customers, debts register, payments, receipts
reports/      report builder, email, send_reports command
templates/    HTML (Tailwind)     static/  logo + POS JavaScript + compiled CSS
assets/       Tailwind input file     tailwind.config.js, package.json: the CSS build
```

## Building the CSS (Tailwind, with Node)

Needs Node 18+ on your own computer (not on the server). From the project folder:

```bash
npm install
npm run build:css      # one-off, minified -> static/css/tailwind.css
npm run watch:css      # while developing: rebuilds on every save
```

* While `static/css/tailwind.css` does not exist, the pages use the Tailwind CDN automatically. Once the file exists they use it and the CDN is no longer loaded.
* Run `npm run build:css` again whenever you add or change Tailwind classes in templates, `static/js/pos.js`, or the Python files that add classes (`core/forms.py`, `core/templatetags/ui.py`). A class that was never built shows up unstyled. Always rebuild before pushing.
* **Commit `static/css/tailwind.css`** to GitHub. Then the server needs no Node: pull, `collectstatic`, restart. `node_modules/` is git-ignored.
* To build on GitHub Actions instead, ignore the CSS file and add a step that runs `npm ci && npm run build:css` before deploying.

## Production notes

* Set `DEBUG=0`, a long random `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, and serve with Gunicorn + Nginx over HTTPS; run `python manage.py collectstatic` (add WhiteNoise or let Nginx serve `/static/`).
* Build the stylesheet before deploying (see **Building the CSS** below). Until `static/css/tailwind.css` exists the pages fall back to the Tailwind CDN, which is for development only.
* Back up the database daily.
* Money is stored as whole FCFA integers (no decimals).
* Notifications are sent from a background thread inside the web process. That is fine for one shop; for the multi-tenant version move them to Celery.

## Next step: multi-tenancy

When you're ready for the SaaS version, every business-owned model (`Product`, `StockMovement`, `Sale`, `Customer`, `Debt`, `DiscountRequest`, `CompanySettings`, `ReportLog`, …) gets a `tenant` foreign key with a central tenant-aware manager/middleware (or `django-tenants`). `services.py` modules already isolate the business logic, which makes that change small.


> **Upgrading to the neighbor-items version?** This update adds a `Neighbor` table and new columns on sales and sale items. Run `python manage.py makemigrations` and `python manage.py migrate` again. Existing sales keep working (their neighbor total is 0). If you were only testing, you can instead delete `db.sqlite3` and the `migrations/0*.py` files in each app and re-run the Quick start.
