/* HenZion POS: cart, split payments, discount approval polling, credit details. */
(function () {
  'use strict';

  const root = document.getElementById('pos');
  const PRODUCTS = JSON.parse(document.getElementById('products-data').textContent);
  const IS_OWNER = root.dataset.owner === '1';
  const VAT = parseFloat(root.dataset.vat) || 0;
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const $ = (id) => document.getElementById(id);
  const METHODS = ['cash', 'mtn_momo', 'orange_money', 'credit'];
  const prefillEl = document.getElementById('prefill-data');
  const PREFILL = prefillEl ? JSON.parse(prefillEl.textContent) : null;
  const PAYING_UNPAID = !!(PREFILL && PREFILL.mode === 'pay');

  let cart = [];
  let saleUuid = makeUuid(); // reused until the sale succeeds, so a double-click can't create two sales
  let proformaUuid = makeUuid(); // same protection for the unpaid invoice button
  let submitting = false;
  let touched = false; // false = the cash box simply follows the total (the common one-method sale)

  function makeUuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }
  const lineTotal = (l) => Math.round(l.qty * l.price); // same rounding as the server (a half of an odd price rounds up)
  const fmt = (n) => Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const amountInput = (m) => document.querySelector('input[data-amount="' + m + '"]');
  const refInput = (m) => document.querySelector('input[data-ref="' + m + '"]');
  const amountOf = (m) => Math.max(0, parseInt(amountInput(m).value, 10) || 0);
  const paidSum = () => METHODS.reduce((s, m) => s + amountOf(m), 0);

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* not json */ }
    return data;
  }

  // ---------------------------------------------------------------- product list
  // Category dropdown: only categories that have products.
  (function fillCategories() {
    const names = Array.from(new Set(PRODUCTS.map((p) => p.category).filter(Boolean))).sort((a, b) => a.localeCompare(b));
    const sel = $('cat-filter');
    names.forEach((n) => { const o = document.createElement('option'); o.value = n; o.textContent = n; sel.appendChild(o); });
    if (PRODUCTS.some((p) => !p.category)) { const o = document.createElement('option'); o.value = '__none__'; o.textContent = 'No category'; sel.appendChild(o); }
  })();

  function matching() {
    const q = $('search').value.trim().toLowerCase();
    const cat = $('cat-filter').value;
    return PRODUCTS.filter((p) => {
      if (cat === '__none__' ? p.category : (cat && p.category !== cat)) return false;
      return !q || (p.name + ' ' + p.sku + ' ' + p.category).toLowerCase().includes(q);
    });
  }

  function renderProducts() {
    const all = matching();
    const list = all.slice(0, 200);
    $('product-count').textContent = all.length > list.length
      ? `Showing the first ${list.length} of ${all.length}. Type more to narrow it down.`
      : `${all.length} product${all.length === 1 ? '' : 's'}`;
    $('product-list').innerHTML = list.map((p) => {
      const out = p.stock <= 0;
      const canOpen = out && cartonAvailable(p);
      return `<button type="button" data-id="${p.id}" ${out && !canOpen ? 'disabled' : ''}
        class="rounded-lg border px-3 py-2 text-left text-sm ${out && !canOpen ? 'bg-slate-50 opacity-50' : 'hover:border-pink-400 hover:bg-pink-50'}">
        <div class="font-medium leading-tight">${esc(p.name)}</div>
        <div class="mt-0.5"><span class="rounded-full ${p.category ? 'bg-sky-100 text-sky-800' : 'bg-slate-100 text-slate-500'} px-2 py-0.5 text-[11px] font-medium">${p.category ? esc(p.category) : 'No category'}</span></div>
        <div class="mt-1 flex justify-between text-xs text-slate-500"><span>${esc(p.sku)}</span><span>${out ? (canOpen ? 'shelf empty · open a carton' : 'out of stock') : p.stock + ' in stock'}</span></div>
        <div class="mt-1 font-semibold text-brand-navy">${fmt(p.price)} FCFA</div></button>`;
    }).join('') || '<p class="text-sm text-slate-500">No product matches.</p>';
  }

  $('product-list').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-id]');
    if (btn) addProduct(parseInt(btn.dataset.id, 10));
  });
  $('search').addEventListener('input', renderProducts);
  $('cat-filter').addEventListener('change', renderProducts);
  $('search').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const q = $('search').value.trim().toLowerCase();
    if (!q) return;
    // Enter adds an exact code match, or the only product left; with several same-named products it
    // does nothing, so the cashier picks the right category herself.
    const exact = PRODUCTS.find((p) => p.sku.toLowerCase() === q);
    const found = matching();
    const pick = exact || (found.length === 1 ? found[0] : null);
    if (pick && (pick.stock > 0 || cartonAvailable(pick))) { addProduct(pick.id); $('search').value = ''; renderProducts(); }
  });

  // ---------------------------------------------------------------- cart
  // A retail listing is fed from cartons. When the shelf is short and a whole carton is left in the
  // store, ask the cashier; if she agrees the carton is opened and its pieces are added to the shelf.
  function cartonAvailable(p) {
    return !!(p.carton && p.carton.stock - stockUsedBy(p.carton.id) >= 1);
  }

  function applyStock(res) {
    PRODUCTS.forEach((x) => {
      if (x.id === res.carton_id) x.stock = res.carton_stock;
      if (x.id === res.retail_id) x.stock = res.retail_stock;
      if (x.carton && x.carton.id === res.carton_id) x.carton.stock = res.carton_stock;
    });
    cart.forEach((l) => {
      if (l.kind === 'neighbor') return;
      if (l.id === res.carton_id) l.stock = res.carton_stock;
      if (l.id === res.retail_id) l.stock = res.retail_stock;
    });
    renderProducts();
  }

  async function ensureShelf(p, wanted) {
    while (p.stock < wanted && cartonAvailable(p)) {
      const ok = window.confirm(
        `Only ${p.stock} of ${p.name} left on the shelf.\n\nOpen a carton (${p.carton.name}) and add ${p.carton.pieces} pieces to the shelf?`);
      if (!ok) return false;
      const res = await postJson(root.dataset.openCartonUrl, { product_id: p.carton.id, count: 1 });
      if (!res.ok) { showError(res.error || 'Could not open the carton.'); return false; }
      applyStock(res);
    }
    return p.stock >= wanted;
  }

  async function addProduct(id) {
    const p = PRODUCTS.find((x) => x.id === id);
    if (!p) return;
    const line = cart.find((l) => l.id === id && l.status !== 'pending');
    const wanted = (line ? line.qty : 0) + 1;
    if (!(await ensureShelf(p, wanted))) {
      if (p.stock >= (line ? line.qty : 0) + (p.half ? 0.5 : 1) && p.half) {
        // only half a carton left: sell that half
        if (line) line.qty += 0.5; else cart.push(newLine(p, 0.5));
        hideError(); renderCart(); return;
      }
      showError(`Only ${p.stock} of ${p.name} in stock.`);
      return;
    }
    if (line) line.qty += 1; else cart.push(newLine(p, 1));
    hideError();
    renderCart();
  }

  function newLine(p, qty) {
    return { id: p.id, name: p.name, category: p.category, list: p.price, price: p.price, qty, stock: p.stock,
             half: !!p.half, requestId: null, status: 'ok', note: '' };
  }

  function stockUsedBy(productId) {
    return cart.filter((l) => l.kind !== 'neighbor' && l.id === productId).reduce((s, l) => s + l.qty, 0);
  }

  function renderCart() {
    $('cart-empty').classList.toggle('hidden', cart.length > 0);
    $('cart-body').innerHTML = cart.map((l, i) => {
      if (l.kind === 'neighbor') {
        // An item fetched from a neighbor: no stock, no approval, but a loss warning if sold below cost.
        const loss = l.price < l.cost;
        return `<tr class="border-t align-top bg-violet-50/60">
          <td class="py-2 pr-2"><div class="font-medium">${esc(l.name)} <span class="ml-1 rounded bg-violet-100 px-1.5 py-0.5 text-xs font-semibold text-violet-700">Neighbor</span></div>
            <div class="text-xs text-slate-500">from ${esc(l.neighbor)} · they charge ${fmt(l.cost)} each</div>
            ${loss ? `<div class="text-xs font-medium text-red-600">Below the neighbor's price: loss of ${fmt((l.cost - l.price) * l.qty)} FCFA</div>` : ''}</td>
          <td class="py-2 pr-2"><input type="number" min="1" value="${l.qty}" data-i="${i}" data-f="qty" class="w-16 rounded border border-slate-300 px-2 py-1"></td>
          <td class="py-2 pr-2"><input type="number" min="1" value="${l.price}" data-i="${i}" data-f="price" class="w-28 rounded border border-slate-300 px-2 py-1"></td>
          <td class="py-2 text-right font-medium">${fmt(lineTotal(l))}</td>
          <td class="py-2 text-right"><button type="button" data-i="${i}" data-f="remove" class="text-red-500 hover:text-red-700" title="Remove">✕</button></td></tr>`;
      }
      const discounted = l.price < l.list;
      const stateNote =
        l.status === 'pending' ? '<div class="text-xs text-amber-600">⏳ waiting for boss…</div>' :
        l.status === 'declined' ? '<div class="text-xs text-red-600">Boss kept the original price</div>' :
        l.status === 'expired' ? '<div class="text-xs text-red-600">No answer in time: original price kept</div>' :
        discounted ? '<div class="text-xs text-emerald-600">✓ approved</div>' :
        l.price > l.list ? `<div class="text-xs text-sky-600">Above list price (${fmt(l.list)})</div>` : '';
      return `<tr class="border-t align-top">
        <td class="py-2 pr-2"><div class="font-medium">${esc(l.name)}</div>
          ${l.category ? `<div class="text-xs text-slate-500">${esc(l.category)}</div>` : ''}${stateNote}</td>
        <td class="py-2 pr-2"><input type="number" min="${l.half ? 0.5 : 1}" step="${l.half ? 0.5 : 1}" value="${l.qty}" data-i="${i}" data-f="qty" class="${l.half ? 'w-20' : 'w-16'} rounded border border-slate-300 px-2 py-1">
          ${l.half ? `<button type="button" data-i="${i}" data-f="half" class="mt-1 block rounded border border-sky-300 bg-sky-50 px-2 py-0.5 text-xs font-medium text-sky-700" title="Sell half a carton">½ carton</button>` : ''}</td>
        <td class="py-2 pr-2">
          ${discounted ? `<div class="text-xs text-slate-400 line-through">${fmt(l.list)}</div>` : ''}
          <input type="number" min="0" value="${l.price}" data-i="${i}" data-f="price" ${l.status === 'pending' ? 'disabled' : ''}
            class="w-28 rounded border px-2 py-1 ${discounted ? 'border-emerald-400 bg-emerald-50' : 'border-slate-300'}"></td>
        <td class="py-2 text-right font-medium">${fmt(lineTotal(l))}</td>
        <td class="py-2 text-right"><button type="button" data-i="${i}" data-f="remove" class="text-red-500 hover:text-red-700" title="Remove">✕</button></td></tr>`;
    }).join('');
    updateTotals();
  }

  $('cart-body').addEventListener('change', async (e) => {
    const el = e.target;
    const l = cart[parseInt(el.dataset.i, 10)];
    if (!l) return;
    if (l.kind === 'neighbor') {
      if (el.dataset.f === 'qty') {
        l.qty = Math.max(1, parseInt(el.value, 10) || 1);
      } else if (el.dataset.f === 'price') {
        const v = parseInt(el.value, 10);
        if (v > 0) l.price = v; else showError('Enter the price you are selling it for (more than 0).');
      }
      renderCart();
      return;
    }
    if (el.dataset.f === 'qty') {
      let q = parseFloat(el.value);
      q = l.half ? Math.round(q * 2) / 2 : Math.round(q);
      if (!q || q < (l.half ? 0.5 : 1)) q = l.half ? 0.5 : 1;
      const others = stockUsedBy(l.id) - l.qty;
      const p = PRODUCTS.find((x) => x.id === l.id);
      if (p && q + others > p.stock) await ensureShelf(p, q + others);
      const available = p ? p.stock : l.stock;
      if (q + others > available) {
        q = Math.max(l.half ? 0.5 : 1, available - others);
        showError(`Only ${available} of ${l.name} in stock.`);
      }
      l.stock = available;
      l.qty = q;
      renderCart();
    } else if (el.dataset.f === 'price') {
      changePrice(l, parseInt(el.value, 10));
    }
  });
  $('cart-body').addEventListener('click', (e) => {
    const half = e.target.closest('button[data-f="half"]');
    if (half) {
      const l = cart[parseInt(half.dataset.i, 10)];
      if (l && l.status !== 'pending') { l.qty = 0.5; renderCart(); }
      return;
    }
    const btn = e.target.closest('button[data-f="remove"]');
    if (!btn) return;
    cart.splice(parseInt(btn.dataset.i, 10), 1);
    renderCart();
  });

  function totals() {
    const subtotal = cart.reduce((s, l) => s + lineTotal(l), 0);
    const vat = Math.round((subtotal * VAT) / 100);
    return { subtotal, vat, total: subtotal + vat };
  }

  function updateTotals() {
    const t = totals();
    $('subtotal').textContent = fmt(t.subtotal);
    $('vat').textContent = fmt(t.vat);
    $('vat-row').classList.toggle('hidden', VAT === 0);
    $('total').textContent = fmt(t.total);
    if (!touched) amountInput('cash').value = t.total; // plain one-method sale: cash covers everything
    updatePaymentUi();
  }

  // ---------------------------------------------------------------- items from a neighbor
  $('n-toggle').addEventListener('click', () => $('n-box').classList.toggle('hidden'));
  $('n-add').addEventListener('click', () => {
    const name = $('n-name').value.trim();
    const neighbor = $('n-neighbor').value.trim();
    const qty = parseInt($('n-qty').value, 10) || 0;
    const cost = parseInt($('n-cost').value, 10);
    const price = parseInt($('n-price').value, 10);
    if (!name) { showError('Enter the name of the item.'); return; }
    if (!neighbor) { showError('Enter which neighbor you are taking it from.'); return; }
    if (qty < 1) { showError('Quantity must be at least 1.'); return; }
    if (isNaN(cost) || cost < 0) { showError("Enter the neighbor's price for the item."); return; }
    if (isNaN(price) || price <= 0) { showError('Enter the price you are selling it for.'); return; }
    cart.push({ kind: 'neighbor', name, neighbor, cost, price, qty, status: 'ok' });
    hideError();
    $('n-name').value = ''; $('n-qty').value = 1; $('n-cost').value = ''; $('n-price').value = ''; // keep the neighbor: often several items from the same one
    renderCart();
  });

  // ---------------------------------------------------------------- split payments
  function updatePaymentUi() {
    const total = totals().total;
    const sum = paidSum();
    const remaining = total - sum;

    $('paid-sum').textContent = fmt(sum);
    const box = $('remaining-box');
    if (total === 0) {
      box.textContent = '';
    } else if (remaining === 0) {
      box.textContent = '✓ Covers the total';
      box.className = 'font-semibold text-emerald-700';
    } else if (remaining > 0) {
      box.textContent = 'Still to cover: ' + fmt(remaining) + ' FCFA';
      box.className = 'font-semibold text-amber-700';
    } else {
      box.textContent = 'Too much by ' + fmt(-remaining) + ' FCFA';
      box.className = 'font-semibold text-red-700';
    }

    ['mtn_momo', 'orange_money'].forEach((m) => refInput(m).classList.toggle('hidden', amountOf(m) === 0));

    const credit = amountOf('credit');
    $('credit-box').classList.toggle('hidden', credit === 0);
    $('credit-amount').textContent = fmt(credit);
    document.querySelectorAll('.req').forEach((s) => s.classList.toggle('hidden', credit === 0));
    $('cust-hint').textContent = credit > 0 ? '(required because part is on credit)' : '(optional, printed on the invoice)';

    const pending = cart.some((l) => l.status === 'pending');
    $('pending-box').classList.toggle('hidden', !pending);
    $('sell-btn').disabled = cart.length === 0 || pending || submitting || total <= 0 || remaining !== 0;
    if ($('pf-btn')) $('pf-btn').disabled = cart.length === 0 || pending || submitting || total <= 0;
  }

  document.querySelectorAll('input[data-amount]').forEach((input) => {
    input.addEventListener('input', () => { touched = true; updatePaymentUi(); });
  });
  document.querySelectorAll('button[data-fill]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.fill;
      const others = METHODS.filter((m) => m !== target).reduce((s, m) => s + amountOf(m), 0);
      amountInput(target).value = Math.max(0, totals().total - others);
      touched = true;
      updatePaymentUi();
    });
  });
  $('pay-reset').addEventListener('click', () => {
    touched = false;
    METHODS.forEach((m) => { amountInput(m).value = 0; });
    updateTotals();
  });

  // ---------------------------------------------------------------- discount approval
  async function changePrice(line, value) {
    if (isNaN(value) || value < 0) { renderCart(); return; }
    if (value >= line.list) {
      line.price = value; // at or above the list price: allowed for everyone, no approval needed
      line.requestId = null; line.status = 'ok';
      renderCart();
      return;
    }
    let reason = '';
    if (!IS_OWNER) {
      reason = window.prompt(`Reason for reducing ${line.name} from ${fmt(line.list)} to ${fmt(value)} FCFA? (optional)\nThe boss will be asked to confirm.`);
      if (reason === null) { renderCart(); return; } // cashier cancelled
    }
    line.status = 'pending';
    renderCart();
    const res = await postJson(root.dataset.requestUrl, { product_id: line.id, price: value, quantity: line.qty, reason: reason });
    if (!res.ok) {
      line.status = 'declined'; line.price = line.list;
      showError(res.error || 'Could not send the request.');
      renderCart();
      return;
    }
    handleDecision(line, value, res);
  }

  function handleDecision(line, value, res) {
    if (res.status === 'approved') { applyApproval(line, value, res.id); return; }
    if (res.status === 'declined' || res.status === 'expired') { applyRefusal(line, res.status); return; }
    // still pending: poll every 3 s until the boss answers or the request expires
    const deadline = Date.now() + (res.expires_in + 5) * 1000;
    const timer = setInterval(async () => {
      if (!cart.includes(line)) { clearInterval(timer); return; }
      try {
        const r = await fetch(root.dataset.statusUrl.replace('__ID__', res.id), { headers: { Accept: 'application/json' } });
        const s = await r.json();
        if (s.status === 'approved') { clearInterval(timer); applyApproval(line, value, res.id); }
        else if (s.status === 'declined' || s.status === 'expired') { clearInterval(timer); applyRefusal(line, s.status); }
        else if (Date.now() > deadline) { clearInterval(timer); applyRefusal(line, 'expired'); }
      } catch (err) { /* network blip: keep polling */ }
    }, 3000);
  }
  function applyApproval(line, value, id) {
    line.price = value; line.requestId = id; line.status = 'ok';
    renderCart();
  }
  function applyRefusal(line, status) {
    line.price = line.list; line.requestId = null; line.status = status;
    renderCart();
  }

  // ---------------------------------------------------------------- submit
  function showError(msg) { const b = $('error-box'); b.textContent = msg; b.classList.remove('hidden'); b.scrollIntoView({ block: 'nearest' }); }
  function hideError() { $('error-box').classList.add('hidden'); }

  function customerPayload() {
    return {
      name: $('c-name').value.trim(), phone: $('c-phone').value.trim(), address: $('c-address').value.trim(),
      niu: $('c-niu').value.trim(), whatsapp: $('c-whatsapp').value.trim(), id_number: $('c-id').value.trim(),
      guarantor_name: $('c-gname').value.trim(), guarantor_phone: $('c-gphone').value.trim(),
    };
  }
  const itemsPayload = () => cart.filter((l) => l.kind !== 'neighbor').map((l) => ({
    product_id: l.id, quantity: l.qty, price: l.price, request_id: l.requestId, proforma_item_id: l.pfItemId || null,
  }));
  const sourcedPayload = () => cart.filter((l) => l.kind === 'neighbor').map((l) => ({
    name: l.name, neighbor: l.neighbor, quantity: l.qty, cost: l.cost, price: l.price,
  }));

  // ---------------------------------------------------------------- unpaid invoice (not a sale)
  if ($('pf-btn')) {
    $('pf-btn').addEventListener('click', async () => {
      hideError();
      if (!$('c-name').value.trim()) {
        showError('Enter the customer or company name first: it is printed on the unpaid invoice.');
        $('c-name').focus();
        return;
      }
      submitting = true; updatePaymentUi();
      $('pf-btn').textContent = 'Saving…';
      const res = await postJson(root.dataset.proformaUrl, {
        client_uuid: proformaUuid,
        valid_days: parseInt($('pf-days').value, 10) || 7,
        hide_prices: $('pf-hide') ? $('pf-hide').checked : false,
        notes: $('notes').value.trim(),
        customer: customerPayload(),
        items: itemsPayload(),
        sourced_items: sourcedPayload(),
      });
      if (res.ok) { window.location = res.redirect; return; }
      submitting = false;
      $('pf-btn').textContent = 'Create unpaid invoice';
      showError(res.error || 'Something went wrong. Please try again.');
      updatePaymentUi();
    });
  }

  $('sell-btn').addEventListener('click', async () => {
    hideError();
    if (amountOf('credit') > 0) {
      if (!$('c-name').value.trim() || !$('c-phone').value.trim() || !$('c-address').value.trim()) { showError('Part of this sale is on credit, so the customer\'s name, phone and address are needed.'); return; }
      if (!$('due-date').value) { showError('Choose the date the customer promises to pay the credit.'); return; }
    }
    submitting = true; updatePaymentUi();
    $('sell-btn').textContent = 'Saving…';
    const payload = {
      client_uuid: saleUuid,
      proforma_id: PAYING_UNPAID ? PREFILL.id : null,
      payments: METHODS.filter((m) => amountOf(m) > 0).map((m) => {
        const ref = refInput(m);
        return { method: m, amount: amountOf(m), reference: ref ? ref.value.trim() : '' };
      }),
      due_date: $('due-date').value,
      notes: $('notes').value.trim(),
      customer: customerPayload(),
      items: itemsPayload(),
      sourced_items: sourcedPayload(),
    };
    const res = await postJson(root.dataset.sellUrl, payload);
    if (res.ok) { window.location = res.redirect; return; }
    submitting = false;
    $('sell-btn').textContent = 'Complete sale';
    showError(res.error || 'Something went wrong. Please try again.');
    updatePaymentUi();
  });

  // ---------------------------------------------------------------- load an unpaid invoice into the till
  function loadPrefill() {
    if (!PREFILL) return;
    const c = PREFILL.customer || {};
    $('c-name').value = c.name || ''; $('c-phone').value = c.phone || ''; $('c-address').value = c.address || ''; $('c-niu').value = c.niu || '';
    $('notes').value = PREFILL.notes || '';
    const trimmed = [];
    PREFILL.items.forEach((it) => {
      const p = PRODUCTS.find((x) => x.id === it.product_id);
      if (!p) return;
      if (p.stock <= 0) { trimmed.push(`${p.name} is out of stock and was left out`); return; }
      const qty = Math.min(it.qty, p.stock);
      if (qty < it.qty) trimmed.push(`${p.name}: only ${p.stock} in stock (invoice says ${it.qty})`);
      cart.push({ id: p.id, name: p.name, category: p.category, list: p.price, price: it.price, qty, stock: p.stock, half: !!p.half,
                  requestId: null, status: 'ok', note: '', pfItemId: it.item_id });
    });
    PREFILL.sourced.forEach((n) => cart.push({ kind: 'neighbor', name: n.name, neighbor: n.neighbor, cost: n.cost, price: n.price, qty: n.qty, status: 'ok' }));
    if (trimmed.length) showError('Check the quantities: ' + trimmed.join('; ') + '.');
  }

  // go
  loadPrefill();
  renderProducts();
  renderCart();
})();
