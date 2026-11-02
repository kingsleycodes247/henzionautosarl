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

  let cart = [];
  let saleUuid = makeUuid(); // reused until the sale succeeds, so a double-click can't create two sales
  let submitting = false;
  let touched = false; // false = the cash box simply follows the total (the common one-method sale)

  function makeUuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }
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
  function renderProducts() {
    const q = $('search').value.trim().toLowerCase();
    const list = PRODUCTS.filter((p) => !q || (p.name + ' ' + p.sku + ' ' + p.category).toLowerCase().includes(q)).slice(0, 60);
    $('product-list').innerHTML = list.map((p) => {
      const out = p.stock <= 0;
      return `<button type="button" data-id="${p.id}" ${out ? 'disabled' : ''}
        class="rounded-lg border px-3 py-2 text-left text-sm ${out ? 'bg-slate-50 opacity-50' : 'hover:border-pink-400 hover:bg-pink-50'}">
        <div class="font-medium leading-tight">${esc(p.name)}</div>
        <div class="mt-1 flex justify-between text-xs text-slate-500"><span>${esc(p.sku)}</span><span>${out ? 'out of stock' : p.stock + ' in stock'}</span></div>
        <div class="mt-1 font-semibold text-brand-navy">${fmt(p.price)} FCFA</div></button>`;
    }).join('') || '<p class="text-sm text-slate-500">No product matches.</p>';
  }

  $('product-list').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-id]');
    if (btn) addProduct(parseInt(btn.dataset.id, 10));
  });
  $('search').addEventListener('input', renderProducts);
  $('search').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const q = $('search').value.trim().toLowerCase();
    if (!q) return;
    const exact = PRODUCTS.find((p) => p.sku.toLowerCase() === q);
    const first = exact || PRODUCTS.find((p) => (p.name + ' ' + p.sku).toLowerCase().includes(q));
    if (first && first.stock > 0) { addProduct(first.id); $('search').value = ''; renderProducts(); }
  });

  // ---------------------------------------------------------------- cart
  function addProduct(id) {
    const p = PRODUCTS.find((x) => x.id === id);
    if (!p) return;
    const line = cart.find((l) => l.id === id && l.status !== 'pending');
    if (line) {
      if (line.qty < p.stock) line.qty += 1; else showError(`Only ${p.stock} of ${p.name} in stock.`);
    } else {
      cart.push({ id: p.id, name: p.name, list: p.price, price: p.price, qty: 1, stock: p.stock, requestId: null, status: 'ok', note: '' });
    }
    hideError();
    renderCart();
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
          <td class="py-2 text-right font-medium">${fmt(l.qty * l.price)}</td>
          <td class="py-2 text-right"><button type="button" data-i="${i}" data-f="remove" class="text-red-500 hover:text-red-700" title="Remove">✕</button></td></tr>`;
      }
      const discounted = l.price < l.list;
      const stateNote =
        l.status === 'pending' ? '<div class="text-xs text-amber-600">⏳ waiting for boss…</div>' :
        l.status === 'declined' ? '<div class="text-xs text-red-600">Boss kept the original price</div>' :
        l.status === 'expired' ? '<div class="text-xs text-red-600">No answer in time: original price kept</div>' :
        discounted ? '<div class="text-xs text-emerald-600">✓ approved</div>' : '';
      return `<tr class="border-t align-top">
        <td class="py-2 pr-2"><div class="font-medium">${esc(l.name)}</div>${stateNote}</td>
        <td class="py-2 pr-2"><input type="number" min="1" max="${l.stock}" value="${l.qty}" data-i="${i}" data-f="qty" class="w-16 rounded border border-slate-300 px-2 py-1"></td>
        <td class="py-2 pr-2">
          ${discounted ? `<div class="text-xs text-slate-400 line-through">${fmt(l.list)}</div>` : ''}
          <input type="number" min="0" value="${l.price}" data-i="${i}" data-f="price" ${l.status === 'pending' ? 'disabled' : ''}
            class="w-28 rounded border px-2 py-1 ${discounted ? 'border-emerald-400 bg-emerald-50' : 'border-slate-300'}"></td>
        <td class="py-2 text-right font-medium">${fmt(l.qty * l.price)}</td>
        <td class="py-2 text-right"><button type="button" data-i="${i}" data-f="remove" class="text-red-500 hover:text-red-700" title="Remove">✕</button></td></tr>`;
    }).join('');
    updateTotals();
  }

  $('cart-body').addEventListener('change', (e) => {
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
      let q = parseInt(el.value, 10);
      if (!q || q < 1) q = 1;
      const others = stockUsedBy(l.id) - l.qty;
      if (q + others > l.stock) { q = Math.max(1, l.stock - others); showError(`Only ${l.stock} of ${l.name} in stock.`); }
      l.qty = q;
      renderCart();
    } else if (el.dataset.f === 'price') {
      changePrice(l, parseInt(el.value, 10));
    }
  });
  $('cart-body').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-f="remove"]');
    if (!btn) return;
    cart.splice(parseInt(btn.dataset.i, 10), 1);
    renderCart();
  });

  function totals() {
    const subtotal = cart.reduce((s, l) => s + l.qty * l.price, 0);
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
      line.price = IS_OWNER ? value : line.list;
      if (!IS_OWNER && value > line.list) showError('A price above the list price is not allowed.');
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
      payments: METHODS.filter((m) => amountOf(m) > 0).map((m) => {
        const ref = refInput(m);
        return { method: m, amount: amountOf(m), reference: ref ? ref.value.trim() : '' };
      }),
      due_date: $('due-date').value,
      notes: $('notes').value.trim(),
      customer: {
        name: $('c-name').value.trim(), phone: $('c-phone').value.trim(), address: $('c-address').value.trim(),
        niu: $('c-niu').value.trim(), whatsapp: $('c-whatsapp').value.trim(), id_number: $('c-id').value.trim(),
        guarantor_name: $('c-gname').value.trim(), guarantor_phone: $('c-gphone').value.trim(),
      },
      items: cart.filter((l) => l.kind !== 'neighbor').map((l) => ({ product_id: l.id, quantity: l.qty, price: l.price, request_id: l.requestId })),
      sourced_items: cart.filter((l) => l.kind === 'neighbor').map((l) => ({ name: l.name, neighbor: l.neighbor, quantity: l.qty, cost: l.cost, price: l.price })),
    };
    const res = await postJson(root.dataset.sellUrl, payload);
    if (res.ok) { window.location = res.redirect; return; }
    submitting = false;
    $('sell-btn').textContent = 'Complete sale';
    showError(res.error || 'Something went wrong. Please try again.');
    updatePaymentUi();
  });

  // go
  renderProducts();
  renderCart();
})();
