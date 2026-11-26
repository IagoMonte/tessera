(() => {
  const f = document.getElementById('f'), erro = document.getElementById('erro'), ok = document.getElementById('ok');
  try { const t = localStorage.getItem('tema'); if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch { /* sem armazenamento */ }
  f.addEventListener('submit', async e => {
    e.preventDefault();
    erro.textContent = ''; ok.disabled = true;
    try {
      const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ senha: f.senha.value, codigo: f.codigo.value.trim() }) });
      if (r.ok) { location.replace('/'); return; }
      erro.textContent = r.status === 429 ? 'Muitas tentativas. Aguarde e tente de novo.' : 'Credenciais inválidas.';
    } catch { erro.textContent = 'Não consegui falar com o painel.'; }
    f.codigo.value = ''; ok.disabled = false;
  });
})();
