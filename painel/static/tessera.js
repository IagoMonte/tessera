/* Tessera — front-end do tty-orquestrador (vanilla JS, sem build).
   Dados: /api/estado (polling), /api/tarefas, /api/config, /api/ferramentas, /api/convite.
   Terminais: xterm.js ligado ao WebSocket do ttyd de cada projeto. Na tailnet vai direto
   (ws://IP:PORTA/ws?arg=<terminal>); fora dela (IP público) passa pelo relé do painel
   (/tty/<maquina>/<projeto>/ws?arg=<terminal>), que alcança o ttyd pela tailnet. */
'use strict';
(() => {
const enc = new TextEncoder();
const sleep = ms => new Promise(r => setTimeout(r, ms));
const MAX = 4;
const NOME_MAQ = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const NOME_PROJ = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || '');

const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* sem armazenamento */ } },
};

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (s, r = document) => r.querySelector(s);
const mobile = () => matchMedia('(max-width:859px)').matches;
const fm = mb => (mb >= 1024 ? (mb / 1024).toFixed(1).replace('.', ',') + ' GB' : Math.round(mb) + ' MB');
const pct = v => Number(v).toFixed(1).replace('.', ',') + '%';
const tilde = (dir, u) => dir.replace(/^\/root(?=\/|$)/, '~').replace(new RegExp('^/home/' + String(u).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?=/|$)'), '~');
const atalho = MAC ? '⌘K' : 'Alt K';

/* ---------- estado ---------- */
const S = {
  est: null, semPainel: false,
  sel: ls.get('sel', null), mtab: ls.get('mtab', 'proj'),
  foco: {}, extras: {}, fechados: ls.get('fechados', {}), vazio: {},
  tema: document.documentElement.dataset.theme || 'dark',
  modal: null, aba: 'geral', consulta: '', palSel: 0, menu: false,
  conviteNome: '', conviteSemRoot: false, convite: '', conviteErro: '', conviteBusy: false,
  tarefas: {}, push: {}, todos: {}, ferr: [],
  toastTimer: null,
};
const chave = (m, p) => `${m}/${p}`;
const maquinas = () => (S.est ? S.est.maquinas : []);
function achar(sel) {
  if (!sel) return null;
  const m = maquinas().find(x => x.nome === sel.m);
  const p = m && m.projetos.find(x => x.nome === sel.p);
  return m && p ? { m, p } : null;
}
const atual = () => achar(S.sel);
const salvarFechados = () => ls.set('fechados', S.fechados);

/* ---------- terminais do projeto atual (modelo VS Code: a tab É a sessão) ---------- */
const SHELLS_TTY = ['bash', 'zsh', 'fish', 'sh', 'dash'];
function nomesTodos(a) {
  const k = chave(a.m.nome, a.p.nome);
  const vivos = a.p.terminais.map(t => t.nome);
  const emVivos = new Set(vivos);
  // extras só valem enquanto pendentes: o que o heartbeat já confirmou sai da lista
  // (senão o nome morto continua em `todos` para sempre e "reabre" sozinho).
  let extras = (S.extras[k] || []).filter(n => !emVivos.has(n));
  if (JSON.stringify(extras) !== JSON.stringify(S.extras[k] || [])) S.extras[k] = extras;
  let todos = [...new Set([...vivos, ...extras])];
  if (!todos.length && !S.vazio[k]) { S.extras[k] = ['main']; todos = ['main']; }  // projeto sem sessão: abre o main
  const ord = n => (n === 'main' ? '' : n);
  todos.sort((x, y) => (ord(x) < ord(y) ? -1 : ord(x) > ord(y) ? 1 : 0));
  // GC: fechado que não existe mais (nem vivo, nem pendente) é esquecido —
  // senão o próximo "+ Novo TTY" reutiliza o nome e nasce invisível.
  let fech = S.fechados[k] || [];
  const emTodos = new Set(todos);
  const limpos = fech.filter(n => emTodos.has(n));
  if (limpos.length !== fech.length) { S.fechados[k] = limpos; salvarFechados(); fech = limpos; }
  return { todos, visiveis: todos.filter(n => !fech.includes(n)) };
}
// cmd fora dos shells = tem processo rodando (ex.: node, claude) → o × pede confirmação
function temProcesso(a, nome) {
  const t = a.p.terminais.find(x => x.nome === nome);
  const cmd = t ? (t.cmd || '') : '';
  return cmd && !SHELLS_TTY.includes(cmd) ? cmd : '';
}
function focoDe(a, visiveis) {
  const f = S.foco[chave(a.m.nome, a.p.nome)];
  return visiveis.includes(f) ? f : visiveis[0];
}
function efetivos(a) {
  const { visiveis } = nomesTodos(a);
  if (mobile()) { const f = focoDe(a, visiveis); return f ? [f] : []; }
  // a grade comporta MAX: o foco sempre entra (senão clicar numa tab além do MAX não mostra nada)
  const f = focoDe(a, visiveis);
  if (visiveis.length <= MAX) return visiveis;
  const resto = visiveis.filter(n => n !== f);
  return f ? [f, ...resto.slice(0, MAX - 1)] : visiveis.slice(0, MAX);
}

/* ---------- tema do xterm ---------- */
function temaXterm() {
  return S.tema === 'light'
    ? { background: '#ffffff', foreground: '#1c1c1f', cursor: '#0a64e6', cursorAccent: '#ffffff', selectionBackground: 'rgba(10,100,230,.22)',
        black: '#1c1c1f', red: '#c7362c', green: '#17803d', yellow: '#9a6700', blue: '#0a64e6', magenta: '#8250df', cyan: '#1b7c83', white: '#6b6c74',
        brightBlack: '#8a8b93', brightRed: '#e5534b', brightGreen: '#1a7f37', brightYellow: '#bf8700', brightBlue: '#218bff', brightMagenta: '#a475f9', brightCyan: '#3192aa', brightWhite: '#3a3a40' }
    : { background: '#0f0f12', foreground: '#ececee', cursor: '#8ab4ff', cursorAccent: '#0f0f12', selectionBackground: 'rgba(138,180,255,.30)',
        black: '#1c1d22', red: '#ff7b72', green: '#9be8b4', yellow: '#f0c674', blue: '#8ab4ff', magenta: '#c8a2ff', cyan: '#79dcdc', white: '#c9cad0',
        brightBlack: '#6c6d75', brightRed: '#ffa198', brightGreen: '#b7f0c9', brightYellow: '#f6d99a', brightBlue: '#a8c8ff', brightMagenta: '#dcc3ff', brightCyan: '#a0ecec', brightWhite: '#ececee' };
}

/* O 100.x só roteia com Tailscale: fora dela o terminal passa pelo relé do painel (/tty/...),
   que alcança o ttyd pela tailnet com a mesma senha do painel. */
function hostSemTailscale() {
  const hn = location.hostname;
  if (hn === 'localhost' || hn === '127.0.0.1' || hn === '::1') return false;
  if (/^100\.(6[4-9]|[78]\d|1[01]\d|12[0-7])\./.test(hn)) return false;  // 100.64.0.0/10
  if (/\.ts\.net\.?$/i.test(hn)) return false;  // MagicDNS: o cliente tem Tailscale
  if (/^fd7a:115c:a1e0:/i.test(hn)) return false;  // IPv6 da Tailscale
  return true;
}

/* ---------- um painel = um terminal ligado ao ttyd ---------- */
class Pane {
  constructor(key, mNome, pNome, nome) {
    this.key = key; this.mNome = mNome; this.pNome = pNome; this.nome = nome;
    this.alvo = null; this.ws = null; this.timer = null; this.tent = 0; this.morto = false; this.aberto = false; this.jaConectou = false; this.forcarRele = false;
    this.dot = h('span', { class: 'dot s-offline' });
    this.cmd = h('span', { class: 'pcmd' });
    this.uso = h('span', { class: 'pusage' });
    this.over = h('div', { class: 'pover' });
    this.body = h('div', { class: 'pbody' }, this.over);
    this.el = h('section', { class: 'pane', onmousedown: () => focar(this.nome) },
      h('div', { class: 'pbar' }, this.dot, h('span', { class: 'ptitle' }, nome), this.cmd, this.uso,
        h('button', { class: 'rl', 'aria-label': `Recarregar o terminal ${nome}`, title: 'Recarregar o terminal: reconecta e redesenha (a sessão tmux continua)', onclick: e => { e.stopPropagation(); this.recarregar(); } }, '⟳'),
        h('button', { class: 'x', 'aria-label': `Encerrar ${nome}`, title: 'Encerrar: mata a sessão tmux (como o × do VS Code)', onclick: e => { e.stopPropagation(); encerrarTerminal(nome); } }, '×')),
      this.body);
    this.term = new Terminal({
      fontFamily: "'Geist Mono', ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: mobile() ? 12 : 13, lineHeight: 1.3,
      cursorBlink: true, scrollback: 5000, allowProposedApi: true, theme: temaXterm(),
    });
    this.fit = new FitAddon.FitAddon();
    this.term.loadAddon(this.fit);
    this.term.onData(d => this.enviar(d));
    this.term.onBinary(d => this.enviar(Uint8Array.from(d, c => c.charCodeAt(0))));
    this.term.onResize(({ cols, rows }) => this.enviarBruto('1' + JSON.stringify({ columns: cols, rows })));
    this.term.attachCustomKeyEventHandler(ev => {
      if (ev.type !== 'keydown') return true;
      if (ev.key.toLowerCase() === 'k' && (ev.metaKey || ev.altKey)) { ev.preventDefault(); abrirModal('pal'); return false; }
      if (ev.ctrlKey && ev.shiftKey && ev.key === 'C' && this.term.hasSelection()) { copiar(this.term.getSelection()); return false; }
      return true;
    });
    this.ro = new ResizeObserver(() => this.ajustar());
  }
  montar() {  // depois de entrar no DOM (o xterm mede o tamanho dos caracteres ao abrir)
    if (this.aberto) return;
    this.aberto = true;
    fontesProntas.then(() => {
      if (this.morto) return;
      this.term.open(this.body);
      this.ro.observe(this.body);
      this.ajustar();
      this.agendar(0);
    });
  }
  ajustar() { try { this.fit.fit(); } catch { /* ainda sem tamanho */ } }
  status(t) { this.over.textContent = t; this.over.style.display = t ? 'flex' : 'none'; }
  agendar(ms) { clearTimeout(this.timer); this.timer = setTimeout(() => this.conectar(), ms); }
  conectar() {
    if (this.morto) return;
    const a = this.alvo;
    if (!a || !a.online) { this.status('máquina offline'); return this.agendar(3000); }
    this.status(this.jaConectou ? 'reconectando…' : 'conectando…');
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const rele = hostSemTailscale() || this.forcarRele;
    const base = rele ? `${proto}://${location.host}/tty/${this.mNome}/${this.pNome}`
                      : `${proto}://${a.ip}:${a.porta}`;
    let ws;
    try { ws = new WebSocket(`${base}/ws?arg=${encodeURIComponent(this.nome)}`, ['tty']); }
    catch { return this.agendar(3000); }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      if (this.jaConectou) this.term.reset();  // o tmux redesenha a tela ao reanexar
      this.jaConectou = true; this.tent = 0; this.status('');
      ws.send(enc.encode(JSON.stringify({ AuthToken: '', columns: this.term.cols, rows: this.term.rows })));
      if (focoPedido === this.key) { this.term.focus(); focoPedido = null; }
    };
    ws.onmessage = ev => { const u = new Uint8Array(ev.data); if (u[0] === 48) this.term.write(u.subarray(1)); };  // '0' = saída
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.morto) return;
      if (!rele && !this.jaConectou) this.forcarRele = true;  // o direto não abriu: tenta o relé
      this.status('desconectado · reconectando…');
      this.agendar(Math.min(1000 * 2 ** this.tent++, 8000));
    };
  }
  recarregar() {
    // reconecta já (sem esperar o backoff), limpa a tela local e deixa o tmux redesenhar; a sessão no servidor não é tocada
    clearTimeout(this.timer);
    this.tent = 0; this.jaConectou = false; this.forcarRele = false;
    if (this.ws) { const w = this.ws; this.ws = null; try { w.close(); } catch { /* já fechado */ } }  // o onclose ignora sockets antigos
    this.term.reset();
    this.ajustar();
    this.status('recarregando…');
    this.conectar();
    if (focoPedido === null) this.term.focus();
  }
  enviarBruto(texto) { if (this.ws && this.ws.readyState === 1) this.ws.send(enc.encode(texto)); }
  enviar(dados) {  // '0' + entrada
    if (!(this.ws && this.ws.readyState === 1)) return;
    const b = typeof dados === 'string' ? enc.encode(dados) : dados;
    const o = new Uint8Array(b.length + 1); o[0] = 48; o.set(b, 1);
    this.ws.send(o);
  }
  atualizar(a, foco) {
    this.alvo = { ip: a.m.ip, porta: a.p.porta, online: a.m.online };
    const t = a.p.terminais.find(x => x.nome === this.nome);
    this.dot.className = 'dot s-' + (t ? t.estado : (a.m.online ? 'ocioso' : 'offline'));
    this.cmd.textContent = t ? t.cmd : '';
    this.uso.textContent = t ? `${pct(t.cpu)} · ${fm(t.mem_mb)}` : '';
    this.el.classList.toggle('focus', foco === this.nome);
  }
  dispose() {
    this.morto = true; clearTimeout(this.timer);
    try { if (this.ws) this.ws.close(); } catch { /* já fechado */ }
    this.ro.disconnect(); this.term.dispose(); this.el.remove();
  }
}
const panes = new Map();
let focoPedido = null;
// o xterm mede a largura dos caracteres ao abrir: espera a webfont (no máximo 1,5 s)
const fontesProntas = Promise.race([
  Promise.all([document.fonts.load("13px 'Geist Mono'"), document.fonts.ready]).catch(() => {}),
  sleep(1500),
]);

function focar(nome) {
  const a = atual(); if (!a) return;
  S.foco[chave(a.m.nome, a.p.nome)] = nome;
  focoPedido = chave(a.m.nome, a.p.nome) + '/' + nome;
  const p = panes.get(focoPedido);
  panes.forEach(x => x.el.classList.toggle('focus', x === p));
  if (p) { p.term.focus(); focoPedido = null; }
  renderHeader(); renderTabs(); if (mobile()) renderMobTty();
}

function reconciliar() {
  const grid = mobile() ? $('#mob-grid') : $('#grid'); if (!grid) return;
  const a = atual();
  if (mobile() && S.mtab !== 'tty') { panes.forEach(p => p.dispose()); panes.clear(); return; }  // o xterm mede o tamanho ao abrir: só com a aba visível
  const quero = a ? efetivos(a) : [];
  const chaves = new Set(quero.map(n => `${a.m.nome}/${a.p.nome}/${n}`));
  for (const [k, p] of panes) if (!chaves.has(k)) { p.dispose(); panes.delete(k); }
  if (!quero.length) {
    panes.forEach(p => p.dispose()); panes.clear();
    grid.style.gridTemplateColumns = 'minmax(0,1fr)';
    grid.replaceChildren(h('div', { class: 'nogrid' },
      h('div', null, a ? 'Nenhum terminal aberto neste projeto.' : (maquinas().length ? (mobile() ? 'Escolha um projeto na aba Projetos.' : 'Escolha um projeto na barra lateral.') : 'Nenhuma máquina registrada ainda.')),
      a ? h('div', { class: 'nogrid-acts' },
        h('button', { class: 'btn primary', onclick: reabrirMain }, 'Reabrir main'),
        h('button', { class: 'btn', onclick: novoTerminal }, '+ Novo TTY'))
        : h('button', { class: 'btn', onclick: () => { S.aba = 'maq'; abrirModal('cfg'); } }, 'Gerar convite de máquina')));
    return;
  }
  const foco = focoDe(a, nomesTodos(a).visiveis);
  const nos = quero.map(n => {
    const k = `${a.m.nome}/${a.p.nome}/${n}`;
    let p = panes.get(k);
    if (!p) { p = new Pane(k, a.m.nome, a.p.nome, n); panes.set(k, p); }
    p.atualizar(a, foco);
    return p;
  });
  grid.style.gridTemplateColumns = quero.length <= 1 ? 'minmax(0,1fr)' : 'repeat(2,minmax(0,1fr))';
  nos.forEach((p, i) => { p.el.style.gridRow = quero.length === 3 && i === 0 ? '1 / span 2' : 'auto'; });
  const atuais = [...grid.children];
  if (atuais.length !== nos.length || atuais.some((c, i) => c !== nos[i].el)) grid.replaceChildren(...nos.map(p => p.el));
  nos.forEach(p => p.montar());
}

/* ---------- ações ---------- */
function selecionar(m, p, term) {
  S.sel = { m, p }; ls.set('sel', S.sel); S.menu = false;
  if (mobile()) { S.mtab = 'tty'; ls.set('mtab', 'tty'); }
  const k = chave(m, p);
  if (term) {
    S.foco[k] = term;
    S.fechados[k] = (S.fechados[k] || []).filter(x => x !== term); salvarFechados();
    focoPedido = `${m}/${p}/${term}`;
  }
  history.replaceState(null, '', `#/${m}/${p}${term ? '/' + term : ''}`);
  S.modal = null; renderTudo();
}
function novoTerminal() {
  const a = atual(); if (!a) return toast('Escolha um projeto antes.');
  if (mobile()) { S.mtab = 'tty'; ls.set('mtab', 'tty'); }
  const k = chave(a.m.nome, a.p.nome);
  const { todos } = nomesTodos(a);
  let n = 2; while (todos.includes('t' + n)) n++;
  const nome = 't' + n;
  (S.extras[k] = S.extras[k] || []).push(nome);
  S.fechados[k] = (S.fechados[k] || []).filter(x => x !== nome); salvarFechados();
  S.vazio[k] = false; S.foco[k] = nome; focoPedido = `${a.m.nome}/${a.p.nome}/${nome}`;
  S.modal = null; renderTudo();
}
// esconder otimista interno: o × agora MATA (encerrarTerminal); isto só tira da tela
// enquanto o kill confirma, ou quando a sessão já morreu no servidor
function fecharVista(nome) {
  const a = atual(); if (!a) return;
  const k = chave(a.m.nome, a.p.nome);
  if (!(S.fechados[k] || []).includes(nome)) { (S.fechados[k] = S.fechados[k] || []).push(nome); salvarFechados(); }
  S.extras[k] = (S.extras[k] || []).filter(x => x !== nome);
  if (!nomesTodos(a).visiveis.length) S.vazio[k] = true;
  renderTudo();
}
function reabrirVista(nome) {
  const a = atual(); if (!a) return;
  const k = chave(a.m.nome, a.p.nome);
  S.fechados[k] = (S.fechados[k] || []).filter(x => x !== nome); salvarFechados();
  S.vazio[k] = false; S.foco[k] = nome; focoPedido = `${a.m.nome}/${a.p.nome}/${nome}`;
  renderTudo();
}
// o main nunca some: reabrir = desmarca o fechado, foca e reconecta
// (o tty-sessao usa `tmux new-session -A`, então um main morto recria limpo no attach)
function reabrirMain() {
  const a = atual(); if (!a) return;
  const k = chave(a.m.nome, a.p.nome);
  if (!(S.extras[k] || []).includes('main') && !a.p.terminais.some(t => t.nome === 'main')) (S.extras[k] = S.extras[k] || []).push('main');
  S.fechados[k] = (S.fechados[k] || []).filter(x => x !== 'main'); salvarFechados();
  S.vazio[k] = false; S.foco[k] = 'main'; focoPedido = `${a.m.nome}/${a.p.nome}/main`;
  S.modal = null; renderTudo();
  toast('Main reaberto.');
}
// × do VS Code: mata a sessão tmux de verdade. Com processo rodando, confirma antes.
async function encerrarTerminal(nome, aForcado) {
  const a = aForcado || atual(); if (!a) return;
  const k = chave(a.m.nome, a.p.nome);
  const cmd = temProcesso(a, nome);
  if (cmd && !confirm(`Encerrar ${a.p.nome}/${nome}?\n\nEstá rodando "${cmd}" — a sessão tmux será morta e o que não foi salvo se perde.${nome === 'main' ? '\nO main recria limpo ao reabrir.' : ''}`)) return;
  S.modal = null; renderModal();
  fecharVista(nome);  // otimista: some já; se falhar, reabrimos abaixo
  toast(`Encerrando ${a.p.nome}/${nome}…`);
  try {
    const t = await tarefa(a.m.nome, 'terminal.encerrar', { projeto: a.p.nome, terminal: nome });
    if (t.estado === 'ok') {
      S.foco[k] = nomesTodos(a).visiveis[0] || 'main';
      toast(`Terminal ${nome} encerrado.${nome === 'main' ? ' Reabra o main quando quiser.' : ''}`);
      await atualizar();
    } else { reabrirVista(nome); toast(`Não encerrou: ${t.saida || t.estado}`); }
  } catch (e) { reabrirVista(nome); toast(e.message); }
}
async function encerrarOutros(nome) {
  const a = atual(); if (!a) return;
  const { visiveis } = nomesTodos(a);
  const outros = visiveis.filter(n => n !== nome);
  if (!outros.length) return toast('Só há este terminal aberto.');
  const rodando = outros.filter(n => temProcesso(a, n));
  if (!confirm(`Encerrar ${outros.length} outro${outros.length > 1 ? 's' : ''} (${outros.join(', ')}) de ${a.p.nome}?${rodando.length ? `\nCom processo rodando: ${rodando.join(', ')}.` : ''}\nSó ${nome} continua.`)) return;
  S.modal = null; renderModal();
  const k = chave(a.m.nome, a.p.nome);
  S.extras[k] = (S.extras[k] || []).filter(x => !outros.includes(x));
  S.fechados[k] = [...new Set([...(S.fechados[k] || []), ...outros])]; salvarFechados();
  S.foco[k] = nome; renderTudo();
  toast(`Encerrando ${outros.length} terminais…`);
  try {
    const res = await Promise.all(outros.map(n => tarefa(a.m.nome, 'terminal.encerrar', { projeto: a.p.nome, terminal: n }, { tentativas: 60 })));
    const falharam = new Set(outros.filter((n, i) => res[i].estado !== 'ok'));
    // os que falharam voltam à tela; os mortos o heartbeat tira de `vivos` e o GC limpa o resto
    S.fechados[k] = (S.fechados[k] || []).filter(x => falharam.has(x));
    salvarFechados();
    const ok = outros.length - falharam.size;
    toast(falharam.size ? `${ok} encerrados, ${falharam.size} falharam (${[...falharam].join(', ')}).` : `Só ${nome} continua.`);
    await atualizar();
  } catch (e) { S.fechados[k] = (S.fechados[k] || []).filter(x => !outros.includes(x)); salvarFechados(); toast(e.message); await atualizar(); }
}
// 🗑 Limpar: mata TODOS os ttys do projeto (main inclusive) e volta pro main limpo
async function limparProjeto() {
  const a = atual(); if (!a) return;
  const { todos } = nomesTodos(a);
  if (!todos.length) { reabrirMain(); return; }
  const rodando = todos.filter(n => temProcesso(a, n));
  if (!confirm(`Limpar todos os TTYs de ${a.p.nome}?\n\n${todos.length} sessão${todos.length > 1 ? 'ões' : ''} (${todos.join(', ')}) ${todos.length > 1 ? 'serão mortas' : 'será morta'}.${rodando.length ? `\nCom processo rodando: ${rodando.join(', ')} — o que não foi salvo se perde.` : ''}\nO main será recriado limpo.`)) return;
  const k = chave(a.m.nome, a.p.nome);
  S.modal = null; renderModal();
  S.extras[k] = []; S.fechados[k] = [...todos]; salvarFechados();
  S.foco[k] = 'main'; renderTudo();
  toast(`Limpando ${a.p.nome}…`);
  try {
    const t = await tarefa(a.m.nome, 'terminal.limpar', { projeto: a.p.nome }, { tentativas: 60 });
    S.fechados[k] = (S.fechados[k] || []).filter(x => x !== 'main'); salvarFechados();
    S.extras[k] = ['main']; S.vazio[k] = false;
    focoPedido = `${a.m.nome}/${a.p.nome}/main`;
    toast(t.estado === 'ok' ? `Projeto limpo. Main recriado. (${t.saida || ''})` : `Não limpou tudo: ${t.saida || t.estado}`);
    await atualizar();
  } catch (e) {
    S.fechados[k] = []; salvarFechados();
    toast(e.message); await atualizar();
  }
}
const limparProjetoAtual = limparProjeto;
async function encerrar(a, nome) { return encerrarTerminal(nome, a); }
async function esquecerProjeto(m, p) {
  if (!confirm(`Esquecer o projeto "${p.nome}" de ${m.nome}?\n\nO agente dele está offline, então isto só tira o projeto da tela (nada é desligado). Se o agente voltar e a pasta ainda estiver configurada, ele reaparece.`)) return;
  try { await api(`/api/maquinas/${encodeURIComponent(m.nome)}/projetos/${encodeURIComponent(p.nome)}`, { method: 'DELETE' }); toast(`Projeto ${p.nome} esquecido.`); await atualizar(); }
  catch (e) { toast(e.message); }
}
async function removerProjeto(m, p) {
  if (!m.online || p.estado === 'offline') return esquecerProjeto(m, p);  // agente offline: não há quem execute o projeto.rm
  if (!confirm(`Remover o projeto "${p.nome}" de ${m.nome}?\n\nO terminal web dele (ttyd) é desligado e ele sai do painel. A pasta (${p.dir}) e as sessões tmux NÃO são apagadas.`)) return;
  toast(`Removendo ${p.nome}…`);
  try {
    const t = await tarefa(m.nome, 'projeto.rm', { nome: p.nome });
    if (t.estado !== 'ok') return toast(`Não removeu: ${t.saida || t.estado}`);
    if (S.sel && S.sel.m === m.nome && S.sel.p === p.nome) { S.sel = null; ls.set('sel', null); history.replaceState(null, '', location.pathname); }
    toast(`Projeto ${p.nome} removido.`);
    await atualizar();
  } catch (e) { toast(e.message); }
}
function setTema(t) {
  S.tema = t; document.documentElement.dataset.theme = t;
  try { localStorage.setItem('tema', t); } catch { /* sem armazenamento */ }
  panes.forEach(p => { p.term.options.theme = temaXterm(); });
  renderModal();
}

async function api(caminho, opcoes) {
  const r = await fetch(caminho, opcoes);
  let j = {}; try { j = await r.json(); } catch { /* corpo vazio */ }
  if (!r.ok) throw new Error(j.erro || `erro ${r.status}`);
  return j;
}
async function tarefa(maquina, tipo, args, { passo = 1000, tentativas = 300, agente } = {}) {
  const { id } = await api('/api/tarefas', { method: 'POST', body: JSON.stringify({ maquina, tipo, args, ...(agente ? { agente } : {}) }) });
  for (let i = 0; i < tentativas; i++) {
    await sleep(passo);
    const t = await api('/api/tarefas/' + id);
    if (['ok', 'erro', 'expirada'].includes(t.estado)) return t;
  }
  return { estado: 'expirada', saida: 'sem resposta do agente' };
}
const sugerirNome = pasta => pasta.replace(/[^A-Za-z0-9_-]/g, '-').replace(/^[^A-Za-z0-9]+/, '').slice(0, 32);
function copiar(texto) {
  const ok = () => toast('Copiado.');
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(texto).then(ok, () => toast('Não consegui copiar.')); return; }
  const ta = h('textarea', { style: { position: 'fixed', opacity: 0 } }); ta.value = texto; document.body.append(ta); ta.select();
  try { document.execCommand('copy'); ok(); } catch { toast('Não consegui copiar.'); }
  ta.remove();
}
function toast(msg) {
  clearTimeout(S.toastTimer);
  let t = $('.toast'); if (!t) { t = h('div', { class: 'toast', role: 'status' }); document.body.append(t); }
  t.textContent = msg;
  S.toastTimer = setTimeout(() => t.remove(), Math.min(15000, Math.max(5000, msg.length * 60)));
}

/* ---------- render: esqueleto ---------- */
function esqueleto() {
  const inputCmd = h('input', { class: 'mob-cmd', id: 'mob-cmd', placeholder: 'comando', autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', enterkeyhint: 'send', 'aria-label': 'Comando',
    onkeydown: e => { if (e.key === 'Enter') { const p = focoPane(); if (p) p.enviar(e.target.value + '\r'); e.target.value = ''; e.preventDefault(); } } });
  $('#app').replaceChildren(h('div', { class: 'stage' },
    h('div', { class: 'shell', id: 'shell' },
      h('aside', { class: 'side' },
        h('div', { class: 'brand' }, h('div', { class: 'logo' }, '>_'), h('b', null, 'Tessera')),
        h('div', { class: 'side-label' }, 'Projetos'),
        h('div', { class: 'side-list', id: 'side-list' }),
        h('div', { class: 'side-foot' }, h('button', { class: 'cfg-btn', onclick: () => abrirModal('cfg') }, h('span', null, '⚙'), 'Configurações'))),
      h('main', null,
        h('header', null,
          h('div', { class: 'htitle', id: 'htitle' }),
          h('button', { class: 'search', onclick: () => abrirModal('pal'), 'aria-label': 'Buscar ou executar' }, h('span', null, 'Buscar ou executar…'), h('kbd', null, atalho)),
          h('button', { class: 'btn', id: 'btn-fac', onclick: () => abrirModal('fac') }, 'Facilitadores'),
          h('button', { class: 'btn', id: 'btn-atu', title: 'Atualiza os scripts de todas as máquinas com a versão do painel', onclick: atualizarTudo }, 'Atualizar'),
          h('button', { class: 'btn', id: 'btn-limpar', title: 'Mata todos os TTYs do projeto e recria o main limpo', onclick: limparProjetoAtual }, '🗑 Limpar'),
          h('button', { class: 'btn primary', id: 'btn-novo', onclick: novoTerminal }, '+ Novo TTY')),
        h('div', { class: 'tabsbar', id: 'tabs' }),
        h('div', { class: 'grid', id: 'grid' }),
        h('footer', { id: 'foot' })),
      // celular: tela cheia, abas embaixo
      h('div', { class: 'mob', id: 'mob' },
        h('header', { class: 'mob-head' },
          h('div', { class: 'logo' }, '>_'),
          h('div', { class: 'mt' }, h('b', { id: 'mob-title' }), h('span', { id: 'mob-sub' })),
          h('button', { class: 'mob-ib', 'aria-label': 'Buscar ou executar', onclick: () => abrirModal('pal') }, '⌕'),
          h('button', { class: 'mob-ib', id: 'mob-add', 'aria-label': 'Novo terminal', onclick: novoTerminal }, '+')),
        h('div', { class: 'mob-body' },
          h('div', { class: 'mob-page', id: 'mob-proj' }),
          h('div', { class: 'mob-tty', id: 'mob-tty' },
            h('div', { class: 'mchips', id: 'mob-chips' }),
            h('div', { class: 'mob-card' }, h('div', { class: 'mob-grid', id: 'mob-grid' }), h('div', { class: 'mob-input' }, h('span', { style: { color: 'var(--acc)' } }, '❯'), inputCmd)),
            h('div', { class: 'mob-keys' }, TECLAS.map(([rot, seq]) => h('button', { type: 'button', onmousedown: e => e.preventDefault(), onclick: () => enviarTecla(seq) }, rot)))),
          h('div', { class: 'mob-page', id: 'mob-fac' }),
          h('div', { class: 'mob-page', id: 'mob-cfg' })),
        h('nav', { class: 'mob-nav', id: 'mob-nav', 'aria-label': 'Seções' })),
      h('div', { id: 'modal-root' }))));
}

function renderSide() {
  const lista = $('#side-list'); if (!lista) return;
  const nos = [];
  if (!maquinas().length) nos.push(h('div', { class: 'empty' }, S.semPainel ? 'Painel inacessível.' : 'Nenhuma máquina ainda. Em Configurações › Máquinas você gera o comando de convite.'));
  maquinas().forEach(m => {
    const memPct = m.ram_mb ? Math.min(100, (m.mem_mb / m.ram_mb) * 100) : 0;
    const sp = (t, c) => h('span', c ? { class: c } : null, t);
    nos.push(h('div', { class: 'mcard' },
      h('div', { class: 'mhead' }, h('span', { class: 'dot', style: { width: '6px', height: '6px', background: m.online ? 'var(--ok)' : 'var(--dim)' } }),
        h('span', { class: 'mname', title: m.ip }, m.nome), h('span', { class: 'mcores' }, m.cores ? `${m.cores} vCPU · ${Math.round(m.ram_mb / 1024)} GB` : (m.online ? '' : 'offline')),
        h('button', { class: 'mcard-add', title: `Novo projeto (pasta) em ${m.nome}`, 'aria-label': `Novo projeto em ${m.nome}`, disabled: !m.online, onclick: () => novoProjetoEm(m.nome) }, '+')),
      h('div', { class: 'mbars' },
        sp('CPU'), h('div', { class: 'bar' }, h('i', { style: { width: Math.min(100, m.cpu) + '%', background: 'var(--acc)' } })), sp(Math.round(m.cpu) + '%', 'val'),
        sp('MEM'), h('div', { class: 'bar' }, h('i', { style: { width: memPct + '%', background: 'var(--fg2)' } })), sp(fm(m.mem_mb), 'val'))));
    m.projetos.forEach(p => {
      const on = S.sel && S.sel.m === m.nome && S.sel.p === p.nome;
      nos.push(h('div', { class: 'proj-wrap' }, h('button', { class: 'proj' + (on ? ' on' : ''), onclick: () => selecionar(m.nome, p.nome), title: p.dir },
        h('span', { class: 'dot pdot s-' + p.estado }),
        h('div', { class: 'pmain' }, h('div', { class: 'pname' }, p.nome), h('div', { class: 'pmeta' }, `${p.branch || '—'} · ${p.terminais.length} tty`)),
        h('div', { class: 'pres' }, h('div', null, pct(p.cpu)), h('div', null, fm(p.mem_mb)))),
        h('button', { class: 'proj-rm', title: p.estado === 'offline' || !m.online ? `Esquecer o projeto ${p.nome} (agente offline)` : `Remover o projeto ${p.nome}`, 'aria-label': `Remover o projeto ${p.nome}`, onclick: () => removerProjeto(m, p) }, '×')));
    });
  });
  lista.replaceChildren(...nos);
}

function renderHeader() {
  const a = atual(), t = $('#htitle'); if (!t) return;
  t.replaceChildren(h('b', null, a ? a.p.nome : 'Tessera'),
    h('span', null, a ? `${a.m.nome} · ${tilde(a.p.dir, a.p.usuario)} · máquina ${Math.round(a.m.cpu)}% cpu · ${fm(a.m.mem_mb)}` : (S.semPainel ? 'painel inacessível' : '')));
  const novo = $('#btn-novo');
  if (novo) novo.classList.toggle('off', !a);
  const limpar = $('#btn-limpar');
  if (limpar) limpar.classList.toggle('off', !a);
}

// tabs estilo VS Code: a tab É a sessão. Clicar foca (e reabre o main fechado);
// o × de cada tab MATA a sessão. Tab apagada = fechada no kill otimista.
function renderTabs() {
  const bar = $('#tabs'); if (!bar) return;
  const a = atual();
  if (!a) { bar.replaceChildren(); bar.hidden = true; return; }
  bar.hidden = false;
  const { todos, visiveis } = nomesTodos(a);
  const emVis = new Set(visiveis);
  const foco = focoDe(a, visiveis);
  const tabs = todos.map(n => {
    const t = a.p.terminais.find(x => x.nome === n);
    const fechada = !emVis.has(n);
    const cmd = t ? t.cmd : '';
    const proc = cmd && !SHELLS_TTY.includes(cmd);
    return h('div', { class: 'tab' + (n === foco && !fechada ? ' on' : '') + (fechada ? ' dim' : ''), role: 'tab', 'aria-selected': String(n === foco && !fechada),
        title: fechada ? `${n} (encerrado · clique para reabrir${n === 'main' ? ' — recria limpo' : ''})` : `${n}${cmd ? ' · ' + cmd : ''}${proc ? ' · rodando' : ''} — clique para focar, × para encerrar` },
      h('button', { class: 'tab-main', onclick: () => { if (fechada) { n === 'main' ? reabrirMain() : reabrirVista(n); } else focar(n); } },
        h('span', { class: 'dot s-' + (t ? t.estado : 'offline'), style: { width: '6px', height: '6px' } }),
        h('span', { class: 'tab-name' }, n),
        proc ? h('span', { class: 'tab-proc' }, '●') : null),
      h('button', { class: 'tab-x', 'aria-label': `Encerrar ${n}`, title: `Encerrar ${n} (mata a sessão)`, onclick: e => { e.stopPropagation(); encerrarTerminal(n); } }, '×'));
  });
  bar.replaceChildren(...tabs,
    h('button', { class: 'tab-add', title: 'Novo terminal', 'aria-label': 'Novo terminal', onclick: novoTerminal }, '+'));
}

function renderFoot() {
  const f = $('#foot'); if (!f) return;
  let rod = 0, tot = 0, aguard = 0;
  maquinas().forEach(m => m.projetos.forEach(p => p.terminais.forEach(t => { tot++; if (t.estado === 'rodando') rod++; if (t.estado === 'aguardando') aguard++; })));
  const a = atual();
  f.replaceChildren(...[
    h('span', { style: { color: 'var(--ok)' } }, `● ${rod} ativos`),
    aguard ? h('span', { style: { color: 'var(--warn)' } }, `● ${aguard} aguardando você`) : null,
    h('span', null, `${tot} sessões · ${maquinas().length} máquinas`),
    S.semPainel ? h('span', { style: { color: 'var(--warn)' } }, 'painel inacessível') : null,
    h('span', { class: 'grow' }),
    h('span', { class: 'hide-m' }, a && a.m.shell ? a.m.shell : 'shell'), h('span', { class: 'hide-m' }, 'UTF-8')].filter(Boolean));
}

function renderTitulo() {
  let aguard = 0;
  maquinas().forEach(m => m.projetos.forEach(p => p.terminais.forEach(t => { if (t.estado === 'aguardando') aguard++; })));
  document.title = (aguard ? `(${aguard}) ` : '') + 'Tessera';
  return aguard;
}

function renderTudo(auto = false) {
  const shell = $('#shell'); if (!shell) return;
  renderTitulo();
  if (mobile()) renderMobile(auto);
  else { renderSide(); renderHeader(); renderTabs(); renderFoot(); }
  reconciliar();
  renderModal(auto);
}

/* ---------- modais ---------- */
function novoProjetoEm(maquina) { S.novo = null; S.novoMaq = maquina; abrirModal('novoproj'); }
function abrirModal(nome) {
  if (mobile() && (nome === 'fac' || nome === 'cfg')) { S.modal = null; renderModal(); return irAba(nome); }
  S.modal = nome; S.consulta = ''; S.palSel = 0;
  if (nome === 'fac') api('/api/ferramentas').then(l => { S.ferr = l; renderModal(); }).catch(() => {});
  renderModal();
}
function fecharModal() { S.modal = null; renderModal(); }
const toggle = (on, label, desc, fn) => h('button', { class: 'row', role: 'switch', 'aria-checked': String(on), onclick: fn },
  h('div', { class: 'grow' }, h('div', null, label), h('div', { class: 'sub' }, desc)), h('div', { class: 'toggle' + (on ? ' on' : '') }, h('i')));
const linha = (k, v) => h('div', { class: 'row' }, h('span', { class: 'grow' }, k), h('span', { class: 'val' }, v));

function renderModal(auto = false) {
  if (mobile() && !auto) renderMobile();  // as abas Facilitadores/Config usam os mesmos conteúdos e handlers
  const raiz = $('#modal-root'); if (!raiz) return;
  const dinamico = S.modal === 'fac' || (S.modal === 'cfg' && S.aba === 'maq');  // listas que dependem do /api/estado
  if (auto && !dinamico) return;
  const ativo = document.activeElement;
  if (auto && raiz.contains(ativo) && ativo.tagName === 'INPUT') return;  // não rouba o foco de quem digita
  const rolagem = (raiz.querySelector('.mbody, .modal.fac') || {}).scrollTop || 0;
  if (!S.modal) { raiz.replaceChildren(); raiz.dataset.focado = ''; return; }
  const fundo = (cls, filho) => h('div', { class: 'overlay ' + cls, onmousedown: e => { if (e.target === e.currentTarget) fecharModal(); } }, filho);
  let el;
  if (S.modal === 'cfg') el = fundo('', modalCfg());
  else if (S.modal === 'fac') el = fundo('', modalFac());
  else if (S.modal === 'novoproj') el = fundo('', modalNovoProj());
  else el = fundo('top', modalPal());
  raiz.replaceChildren(el);
  const rol = raiz.querySelector('.mbody, .modal.fac'); if (rol) rol.scrollTop = rolagem;
  const foco = $('[data-autofocus]', raiz);
  if (foco && raiz.dataset.focado !== S.modal + S.aba) { foco.focus(); raiz.dataset.focado = S.modal + S.aba; }
}

const cfgAtual = () => (S.est ? S.est.config : { notificacoes: {}, telegram: false, convites: false });
function cfgGeral() {
  return [h('div', { class: 'sect' }, 'Aparência'),
    h('div', { class: 'opts' }, [['dark', 'Escuro'], ['light', 'Claro']].map(([k, l]) => h('button', { class: 'opt' + (S.tema === k ? ' on' : ''), onclick: () => setTema(k) }, l))),
    h('div', { class: 'sect' }, 'Painel'),
    h('div', { class: 'rows' }, linha('Endereço', location.host), linha('Sessões persistentes', 'tmux'), mobile() ? null : linha('Atalho da paleta', atalho))];
}
function cfgNotif() {
  const cfg = cfgAtual();
  const nd = { terminou: ['Terminou', 'Comando ou tarefa concluída'], aprovacao: ['Aprovação', 'Processo aguardando confirmação'], pergunta: ['Pergunta', 'CLI pediu uma resposta'],
    erro: ['Erro', 'Saída com falha'], ocioso: ['Ocioso', 'Sessão parada há 10 min'], online: ['Online', 'Máquina entrou na rede'] };
  return [h('div', { class: 'sect' }, 'Avisos no Telegram'),
    h('div', { class: 'rows' }, Object.keys(nd).map(k => toggle(!!cfg.notificacoes[k], nd[k][0], nd[k][1], async () => {
      const novo = !cfg.notificacoes[k]; cfg.notificacoes[k] = novo; renderModal();
      try { await api('/api/config', { method: 'PUT', body: JSON.stringify({ notificacoes: { [k]: novo } }) }); } catch (e) { cfg.notificacoes[k] = !novo; toast(e.message); renderModal(); }
    }))),
    h('div', { class: 'sect' }, 'Bot'),
    h('div', { class: 'rows' }, linha('Token', cfg.telegram ? 'TG_TOKEN · ••••••••' : 'não configurado'), linha('Chat', cfg.telegram ? 'TG_CHAT · configurado' : 'não configurado'),
      cfg.telegram ? null : h('div', { class: 'row' }, h('span', { class: 'sub grow' }, 'Defina TG_TOKEN e TG_CHAT no .env do painel e reinicie o serviço.')))];
}
function cfgMaq() {
  const cfg = cfgAtual();
  return [h('div', { class: 'sectrow' }, h('div', { class: 'sect' }, 'Máquinas na Tailscale')),
    h('div', { class: 'sub', style: { margin: '0 0 12px', fontSize: '12px', lineHeight: '1.5' } }, `O convite abaixo instala uma máquina ou um usuário NOVO. Para adicionar uma pasta a uma máquina que já existe, use o + ao lado dela ${mobile() ? 'na aba Projetos' : 'na barra lateral'}.`),
    h('div', { class: 'sectrow' },
      h('input', { class: 'field', placeholder: 'nome da máquina (ex.: srv-exemplo)', 'aria-label': 'Nome da máquina', value: S.conviteNome, 'data-autofocus': mobile() ? null : '', maxlength: 64,
        oninput: e => { S.conviteNome = e.target.value; }, onkeydown: e => { if (e.key === 'Enter') gerarConvite(); } }),
      h('button', { class: 'mini', disabled: S.conviteBusy || (!cfg.convites && !S.conviteSemRoot), title: cfg.convites || S.conviteSemRoot ? '' : 'TS_API_KEY não configurada no painel', onclick: gerarConvite }, S.conviteBusy ? 'Gerando…' : 'Gerar convite')),
    h('label', { class: 'sub', style: { display: 'flex', gap: '8px', alignItems: 'flex-start', margin: '0 0 12px', fontSize: '12px', lineHeight: '1.5' } },
      h('input', { type: 'checkbox', checked: S.conviteSemRoot ? '' : null, onchange: e => { S.conviteSemRoot = e.target.checked; S.convite = ''; renderModal(); } }),
      h('span', null, 'Sem sudo: para um usuário comum de um servidor que já está na tailnet (ex.: usuário de site em hospedagem). Instala tudo no HOME dele. Use o MESMO nome da máquina para todos os usuários do servidor: cada usuário vira um projeto dela.')),
    S.conviteErro ? h('div', { class: 'sub', style: { color: 'var(--warn)', margin: '0 0 12px', fontSize: '12px' } }, S.conviteErro) : null,
    S.convite ? h('button', { class: 'invite', title: 'Copiar', onclick: () => copiar(S.convite) }, S.convite, h('div', { style: { color: 'var(--mut)', marginTop: '6px' } }, (S.conviteSemRoot ? 'rode como o usuário (sem sudo) · toque para copiar' : 'expira em 1 h · uso único · toque para copiar'))) : null,
    h('div', { class: 'rows' }, maquinas().length ? maquinas().map(m => h('div', { class: 'row' },
      h('span', { class: 'dot', style: { background: m.online ? 'var(--ok)' : 'var(--dim)' } }),
      h('div', { class: 'grow' }, h('div', null, m.nome), h('div', { class: 'mono sub', style: { color: 'var(--dim)', fontSize: '11px' } }, m.ip)),
      h('span', { class: 'sub', style: { margin: 0 } }, visto(m)),
      m.online ? null : h('button', { class: 'mini ghost', title: 'Tirar esta máquina do painel (se o agente voltar, ela reaparece ao rodar o bootstrap)', onclick: () => removerMaquina(m) }, 'remover'))) : h('div', { class: 'row' }, h('span', { class: 'sub' }, 'Nenhuma máquina registrada.')))];
}
function modalCfg() {
  const abas = [['geral', 'Geral'], ['notif', 'Notificações'], ['maq', 'Máquinas']];
  const corpo = S.aba === 'geral' ? cfgGeral() : S.aba === 'notif' ? cfgNotif() : cfgMaq();
  return h('div', { class: 'modal', onmousedown: e => e.stopPropagation(), role: 'dialog', 'aria-label': 'Configurações' },
    h('div', { class: 'mtabs' }, h('h2', null, 'Configurações'), abas.map(([k, l]) => h('button', { class: S.aba === k ? 'on' : '', onclick: () => { S.aba = k; renderModal(); } }, l))),
    h('div', { class: 'mbody' }, corpo));
}
function desdeEpoch(em) {
  const s = Math.max(0, Math.floor(Date.now() / 1000) - em);
  if (s < 60) return 'agora há pouco';
  if (s < 3600) return `há ${Math.floor(s / 60)} min`;
  if (s < 86400) return `há ${Math.floor(s / 3600)} h`;
  return `há ${Math.floor(s / 86400)} d`;
}
function visto(m) {
  if (m.online) return 'agora';
  const s = (S.est ? S.est.agora : 0) - m.visto_em;
  if (!m.visto_em) return 'nunca visto';
  if (s < 60) return `visto há ${s} s`;
  if (s < 3600) return `visto há ${Math.floor(s / 60)} min`;
  if (s < 86400) return `visto há ${Math.floor(s / 3600)} h`;
  return `visto há ${Math.floor(s / 86400)} d`;
}
async function removerMaquina(m) {
  if (!confirm(`Tirar "${m.nome}" do painel? Os projetos e terminais dela somem da tela (as sessões tmux na máquina não são afetadas).`)) return;
  try { await api('/api/maquinas/' + encodeURIComponent(m.nome), { method: 'DELETE' }); toast(`${m.nome} removida do painel.`); await atualizar(); }
  catch (e) { toast(e.message); }
}
async function gerarConvite() {
  const nome = S.conviteNome.trim();
  S.conviteErro = '';
  if (!NOME_MAQ.test(nome)) { S.conviteErro = 'Nome inválido: use letras, números, ponto, _ e -.'; return renderModal(); }
  S.conviteBusy = true; renderModal();
  try { S.convite = (await api('/api/convite', { method: 'POST', body: JSON.stringify({ nome, sem_root: S.conviteSemRoot }) })).comando; }
  catch (e) { S.convite = ''; S.conviteErro = e.message; }
  S.conviteBusy = false; renderModal();
}

function listaFerramentas() {
  const a = atual();
  const tools = S.ferr.map(f => {
    const pk = f.nome;
    return h('div', { class: 'tool' },
      h('div', { class: 'tool-head' },
        h('div', { style: { flex: 1 } }, h('div', { class: 'nm' }, f.nome), h('div', { style: { fontSize: '12px', color: 'var(--mut)', marginTop: '3px' } }, f.desc),
          h('div', { class: 'mono', style: { fontSize: '11px', marginTop: '6px', color: f.config && f.config.tem ? 'var(--ok)' : 'var(--warn)' } },
            f.config && f.config.tem ? `config no painel · enviada ${desdeEpoch(f.config.em)}` : 'sem config no painel: use tty-push numa máquina que já a tenha')),
        h('button', { class: 'mini', disabled: S.todos[pk] === 'executando' || !f.config || !f.config.tem, title: 'tty-add em todas as máquinas online', onclick: () => atualizarFerramentaTodos(f) },
          S.todos[pk] === 'executando' ? 'Atualizando…' : 'Atualizar todos'),
        h('button', { class: 'mini ghost', disabled: !a || !a.m.online || S.push[pk] === 'executando', onclick: () => pushConfig(f, a) },
          S.push[pk] === 'executando' ? 'Enviando…' : S.push[pk] === 'ok' ? 'Enviado ao painel' : 'tty-push · enviar config')),
      maquinas().flatMap(m => {
        // um agente por usuário (modo sem root): cada um instala na própria conta
        const ags = m.agentes && m.agentes.length ? m.agentes : [{ id: '', online: m.online, ferramentas: m.ferramentas || {} }];
        return ags.map(ag => {
          const tem = !!(ag.ferramentas && ag.ferramentas[f.nome]);
          const tk = `${f.nome}:${m.nome}:${ag.id}`, t = S.tarefas[tk];
          const on = m.online && ag.online !== false;
          const status = !on ? 'offline' : t && t.estado === 'executando' ? 'executando…' : t && t.estado === 'erro' ? 'falhou' : tem ? 'instalado' : 'não instalado';
          return h('div', { class: 'tool-row' },
            h('span', { class: 'dot', style: { width: '7px', height: '7px', background: tem ? 'var(--ok)' : 'var(--dim)' } }),
            h('div', { style: { flex: 1 } }, m.nome, ags.length > 1 ? h('span', { class: 'mono', style: { color: 'var(--dim)', fontSize: '11.5px' } }, ` · ${ag.id}`) : null),
            h('div', { class: 'st', title: t && t.saida ? t.saida : '' }, status),
            h('button', { class: 'mini', disabled: !on || (t && t.estado === 'executando'), onclick: () => instalarFerramenta(f, m, ag) }, tem ? 'Atualizar config' : 'tty-add'));
        });
      }));
  });
  return [...(tools.length ? tools : [h('div', { class: 'hint' }, 'Nenhuma ferramenta em facilitadores/ferramentas.')]),
    h('div', { class: 'hint' }, '$ tty-add claude [--sem-config] [--so item1,item2]   ·   $ tty-push claude')];
}
function modalFac() {
  return h('div', { class: 'modal fac', onmousedown: e => e.stopPropagation(), role: 'dialog', 'aria-label': 'Facilitadores' },
    h('div', { class: 'mhd' }, h('b', null, 'Facilitadores'), h('p', null, 'Instale uma ferramenta e leve a configuração dela entre as máquinas.'),
      h('button', { class: 'close', 'aria-label': 'Fechar', onclick: fecharModal }, '×')),
    listaFerramentas());
}
async function instalarFerramenta(f, m, ag, quieto) {
  const usuario = ag.id && ag.id !== 'root' ? ag.id : (m.projetos[0] || {}).usuario;
  if (!usuario) { if (!quieto) toast(`${m.nome} não tem projeto: crie um antes.`); return { ok: false, nome: m.nome, saida: 'sem projeto' }; }
  const tk = `${f.nome}:${m.nome}:${ag.id}`;
  S.tarefas[tk] = { estado: 'executando' }; renderModal();
  try { S.tarefas[tk] = await tarefa(m.nome, 'ferramenta.add', { usuario, ferramenta: f.nome }); } catch (e) { S.tarefas[tk] = { estado: 'erro', saida: e.message }; }
  const ult = (S.tarefas[tk].saida || '').split('\n').map(x => x.trim()).filter(Boolean).slice(-1)[0] || '';
  const aviso = (S.tarefas[tk].saida || '').split('\n').find(x => /SEM CONFIG|NÃO foi aplicada|NÃO foi/.test(x));
  const ok = S.tarefas[tk].estado === 'ok', msg = ok ? (aviso ? aviso.replace(/^•\s*/, '') : ult.replace(/^•\s*/, '')) : (S.tarefas[tk].saida || 'falhou');
  if (!quieto) toast(`tty-add ${f.nome} em ${m.nome}: ${msg}`);
  renderModal();
  return { ok: ok && !aviso, nome: ag.id && ag.id !== 'root' && m.agentes && m.agentes.length > 1 ? `${m.nome}/${ag.id}` : m.nome, saida: msg };
}
// tty-add <ferramenta> em todas as máquinas/agentes online (aplica a config guardada no painel). Não reinicia nada.
async function atualizarFerramentaTodos(f) {
  if (S.todos[f.nome] === 'executando') return;
  const alvos = maquinas().filter(m => m.online).flatMap(m => (m.agentes && m.agentes.length ? m.agentes.filter(a => a.online !== false) : [{ id: '' }]).map(ag => ({ m, ag })));
  if (!alvos.length) return toast('Nenhuma máquina online.');
  S.todos[f.nome] = 'executando'; renderModal();
  toast(`tty-add ${f.nome} em ${alvos.length} agente${alvos.length > 1 ? 's' : ''}…`);
  const res = await Promise.all(alvos.map(({ m, ag }) => instalarFerramenta(f, m, ag, true)));
  S.todos[f.nome] = 'ok'; renderModal();
  const mal = res.filter(r => !r.ok);
  toast(mal.length ? `${f.nome}: ${res.length - mal.length} ok. Problemas: ${mal.map(r => `${r.nome} (${r.saida})`).join('; ')}` : `${f.nome} atualizado em ${res.map(r => r.nome).join(', ')}.`);
}
// Atualiza os scripts (tty-notify, agente, ...) de todos os agentes online com o pacote do painel.
async function atualizarTudo() {
  if (S.atualizando) return;
  const alvos = maquinas().filter(m => m.online).flatMap(m => (m.agentes && m.agentes.length ? m.agentes.filter(a => a.online) : [{ id: '' }]).map(ag => ({ m, ag })));
  if (!alvos.length) return toast('Nenhuma máquina online para atualizar.');
  S.atualizando = true; document.querySelectorAll('#btn-atu').forEach(b => { b.textContent = 'Atualizando…'; b.disabled = true; });
  toast(`Atualizando ${alvos.length} agente${alvos.length > 1 ? 's' : ''}…`);
  const res = await Promise.all(alvos.map(async ({ m, ag }) => {
    const nome = ag.id && ag.id !== 'root' && m.agentes && m.agentes.length > 1 ? `${m.nome}/${ag.id}` : m.nome;
    try {
      const t = await tarefa(m.nome, 'sistema.atualizar', {}, { agente: ag.id || undefined, tentativas: 90 });
      return { nome, ok: t.estado === 'ok', saida: (t.saida || '').split('\n').map(x => x.replace(/^•\s*/, '').trim()).filter(Boolean).join(' · ') };
    } catch (e) { return { nome, ok: false, saida: e.message }; }
  }));
  S.atualizando = false; document.querySelectorAll('#btn-atu').forEach(b => { b.textContent = 'Atualizar'; b.disabled = false; });
  const bem = res.filter(r => r.ok), mal = res.filter(r => !r.ok);
  toast(mal.length
    ? `Atualizados: ${bem.map(r => r.nome).join(', ') || 'nenhum'}. Falharam: ${mal.map(r => `${r.nome} (${r.saida || 'sem resposta'})`).join('; ')}`
    : `Tudo atualizado: ${bem.map(r => `${r.nome}: ${r.saida}`).join(' | ')}`);
}
async function pushConfig(f, a) {
  if (!a) return;
  S.push[f.nome] = 'executando'; renderModal();
  try {
    const t = await tarefa(a.m.nome, 'ferramenta.push', { usuario: a.p.usuario, ferramenta: f.nome });
    S.push[f.nome] = t.estado; if (t.estado !== 'ok') toast(`tty-push ${f.nome}: ${t.saida || t.estado}`);
    else { toast(`Config de ${f.nome} enviada ao painel.`); api('/api/ferramentas').then(l => { S.ferr = l; renderModal(); }).catch(() => {}); }
  } catch (e) { S.push[f.nome] = 'erro'; toast(e.message); }
  renderModal();
}

function modalNovoProj() {
  const m = maquinas().find(x => x.nome === S.novoMaq) || (atual() || {}).m;
  const p0 = m && m.projetos[0];
  const usuarios = m ? [...new Set(m.projetos.map(p => p.usuario).filter(Boolean))] : [];
  const dados = S.novo || (S.novo = { nome: '', dir: p0 ? p0.dir.replace(/\/[^/]*$/, '/') : '/', usuario: p0 ? p0.usuario : '' });
  const campo = (rot, k, ph, af, lista) => h('label', null, rot, h('input', { class: 'field', value: dados[k], placeholder: ph, list: lista || null, 'data-autofocus': af ? '' : null, oninput: e => { dados[k] = e.target.value; } }));

  // ---- autocompletar da pasta: o agente da máquina lista as subpastas (como o usuário); filtramos o prefixo aqui ----
  S.acCache = S.acCache || new Map();
  const ac = { itens: [], estado: 'idle', erro: '', sel: 0, aberto: false, navegou: false, home: '', seq: 0, timer: null, visiveis: [] };
  const lista = h('div', { class: 'ac', role: 'listbox', 'aria-label': 'Sugestões de pasta' });
  lista.hidden = true;
  let campoPasta, campoNome;
  const acFechar = () => { ac.aberto = false; acPintar(); };
  function acPintar() {
    lista.replaceChildren();
    lista.hidden = !ac.aberto;
    if (!ac.aberto) return;
    const v = dados.dir, prefixo = v.slice(v.lastIndexOf('/') + 1).toLowerCase();
    const aviso = t => lista.append(h('div', { class: 'ac-msg' }, t));
    if (!m || !m.online) return aviso('máquina offline');
    if (ac.estado === 'listando') return aviso('listando…');
    if (ac.estado === 'erro') return aviso(ac.erro);
    ac.visiveis = ac.itens.filter(n => n.toLowerCase().startsWith(prefixo)).slice(0, 8);
    ac.sel = Math.min(ac.sel, Math.max(0, ac.visiveis.length - 1));
    if (!ac.visiveis.length) return aviso(ac.itens.length ? 'nenhuma pasta começa assim' : '(sem subpastas)');
    ac.visiveis.forEach((n, i) => lista.append(h('button', { type: 'button', class: 'ac-item' + (i === ac.sel ? ' on' : ''), role: 'option', 'aria-selected': String(i === ac.sel),
      onmousedown: e => { e.preventDefault(); aceitar(n); } }, h('span', { class: 'ac-ico' }, '▸'), n, '/')));
  }
  function acAtualizar() {
    const v = dados.dir, i = v.lastIndexOf('/');
    if (i < 0) { ac.aberto = false; return acPintar(); }
    ac.aberto = true;
    if (!m || !m.online) return acPintar();
    const pai = v.slice(0, i + 1), ocultas = v.slice(i + 1).startsWith('.');
    const usuario = dados.usuario || usuarios[0] || '';
    if (!usuario) { ac.estado = 'erro'; ac.erro = 'informe o usuário para listar as pastas'; return acPintar(); }
    const chave = `${m.nome}|${usuario}|${pai}|${ocultas}`, c = S.acCache.get(chave);
    clearTimeout(ac.timer);
    if (c && Date.now() - c.em < 30000) { ac.seq++; ac.itens = c.itens; ac.home = c.home; ac.estado = 'ok'; return acPintar(); }
    ac.estado = 'listando'; acPintar();
    const seq = ++ac.seq;
    ac.timer = setTimeout(async () => {  // espera o usuário parar de digitar; só pede quando a PASTA-PAI muda
      try {
        const t = await tarefa(m.nome, 'pasta.listar', { dir: pai, usuario, ocultas }, { passo: 150, tentativas: 60 });
        if (seq !== ac.seq) return;  // resposta velha: o campo já mudou
        if (t.estado === 'ok' && t.dados && t.dados.ok) {
          S.acCache.set(chave, { em: Date.now(), itens: t.dados.pastas, home: t.dados.home });
          ac.itens = t.dados.pastas; ac.home = t.dados.home; ac.estado = 'ok';
        } else { ac.estado = 'erro'; ac.erro = (t.dados && t.dados.erro) || t.saida || (t.estado === 'expirada' ? 'sem resposta da máquina' : 'não consegui listar'); }
      } catch (e) { if (seq !== ac.seq) return; ac.estado = 'erro'; ac.erro = e.message; }
      acPintar();
    }, 150);
  }
  function aceitar(nome) {
    const v = dados.dir;
    let pai = v.slice(0, v.lastIndexOf('/') + 1);
    if (pai.startsWith('~/') && ac.home) pai = ac.home + pai.slice(1);  // projeto.add exige caminho absoluto
    dados.dir = pai + nome + '/';
    campoPasta.value = dados.dir;
    if (!dados.nome) { dados.nome = sugerirNome(nome); campoNome.value = dados.nome; }
    ac.sel = 0; ac.navegou = false;
    campoPasta.focus();
    acAtualizar();
  }
  campoPasta = h('input', { class: 'field', value: dados.dir, placeholder: '/home/usuario/dev/meu-projeto', autocomplete: 'off', spellcheck: 'false', role: 'combobox', 'aria-expanded': 'false',
    oninput: e => { dados.dir = e.target.value; ac.sel = 0; ac.navegou = false; acAtualizar(); },
    onfocus: () => acAtualizar(),
    onblur: () => setTimeout(acFechar, 120),
    onkeydown: e => {
      const abertoComItens = ac.aberto && ac.estado === 'ok' && ac.visiveis.length;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (abertoComItens) { e.preventDefault(); ac.navegou = true; ac.sel = (ac.sel + (e.key === 'ArrowDown' ? 1 : -1) + ac.visiveis.length) % ac.visiveis.length; acPintar(); }
        else if (e.key === 'ArrowDown') { e.preventDefault(); acAtualizar(); }
      } else if (e.key === 'Tab' && !e.shiftKey && abertoComItens) { e.preventDefault(); aceitar(ac.visiveis[ac.sel]); }
      else if (e.key === 'Enter' && abertoComItens && ac.navegou) { e.preventDefault(); aceitar(ac.visiveis[ac.sel]); }  // Enter só aceita se navegou; senão envia o formulário
      else if (e.key === 'Escape' && ac.aberto) { e.preventDefault(); e.stopPropagation(); acFechar(); }  // Esc fecha só a lista, não o modal
    } });
  campoNome = h('input', { class: 'field', value: dados.nome, placeholder: 'meu-projeto', 'data-autofocus': '', oninput: e => { dados.nome = e.target.value; } });

  return h('form', { class: 'modal small', onmousedown: e => e.stopPropagation(), 'aria-label': 'Novo projeto', onsubmit: async e => {
    e.preventDefault();
    if (!m) return toast('Nenhuma máquina disponível.');
    if (!NOME_PROJ.test(dados.nome)) return toast('Nome do projeto: letras, números, _ e - (até 32).');
    if (!dados.dir.startsWith('/')) return toast('O diretório deve ser um caminho absoluto.');
    const args = { nome: dados.nome, dir: dados.dir.length > 1 ? dados.dir.replace(/\/+$/, '') : dados.dir }; if (dados.usuario) args.usuario = dados.usuario;
    S.novo = null; fecharModal(); toast(`Criando projeto ${args.nome} em ${m.nome}…`);
    try { const t = await tarefa(m.nome, 'projeto.add', args); toast(t.estado === 'ok' ? `Projeto ${args.nome} criado.` : `Não criou: ${t.saida || t.estado}`); } catch (er) { toast(er.message); }
  } },
  h('div', { class: 'mhd' }, h('b', null, 'Novo projeto'), h('p', null, m ? `uma pasta em ${m.nome}` : 'nenhuma máquina disponível')),
  h('div', { class: 'form' },
    h('label', null, 'Nome', campoNome),
    h('label', { class: 'ac-wrap' }, 'Pasta (já existente; digite e escolha nas sugestões)', campoPasta, lista),
    campo('Usuário (dono da pasta)', 'usuario', 'padrão: dono da pasta', false, 'usuarios-dl'),
    h('datalist', { id: 'usuarios-dl' }, usuarios.map(u => h('option', { value: u }))),
    h('div', { class: 'hint', style: { margin: 0 } }, 'Ou, no terminal da própria máquina, dentro da pasta: tty-projeto add'),
    h('div', { class: 'acts' }, h('button', { type: 'button', class: 'mini ghost', onclick: () => { S.novo = null; fecharModal(); } }, 'Cancelar'), h('button', { type: 'submit', class: 'mini' }, 'Criar'))));
}

function itensPaleta() {
  const q = S.consulta.toLowerCase(), out = [], a = atual(), multi = maquinas().length > 1;
  maquinas().forEach(m => m.projetos.forEach(p => out.push({ kind: 'Projeto', label: p.nome + (multi ? `  ·  ${m.nome}` : ''), run: () => selecionar(m.nome, p.nome) })));
  out.push({ kind: 'Ação', label: 'Novo TTY neste projeto', run: novoTerminal },
    { kind: 'Ação', label: 'Novo projeto…', run: () => novoProjetoEm((atual() || { m: {} }).m.nome || (maquinas()[0] || {}).nome) },
    { kind: 'Ação', label: 'Facilitadores', run: () => abrirModal('fac') },
    { kind: 'Ação', label: 'Atualizar todas as máquinas', run: atualizarTudo },
    { kind: 'Ação', label: 'Configurações', run: () => abrirModal('cfg') },
    { kind: 'Ação', label: S.tema === 'dark' ? 'Tema claro' : 'Tema escuro', run: () => { fecharModal(); setTema(S.tema === 'dark' ? 'light' : 'dark'); } });
  if (a) {
    out.push({ kind: 'Ação', label: 'Recarregar o terminal em foco', run: () => { fecharModal(); const p = focoPane(); if (p) p.recarregar(); } },
      { kind: 'Ação', label: 'Recarregar todos os terminais', run: () => { fecharModal(); panes.forEach(x => x.recarregar()); } });
    const { todos, visiveis } = nomesTodos(a);
    const foco = focoDe(a, visiveis);
    out.push({ kind: 'Ação', label: 'Reabrir main (recria limpo se foi encerrado)', run: () => { S.modal = null; renderModal(); reabrirMain(); } });
    out.push({ kind: 'Ação', label: `🗑 Limpar ${a.p.nome} e voltar ao main…`, run: () => { S.modal = null; renderModal(); limparProjeto(); } });
    out.push({ kind: 'Ação', label: `Remover o projeto ${a.p.nome}…`, run: () => { S.modal = null; renderModal(); removerProjeto(a.m, a.p); } });
    if (foco) out.push({ kind: 'Terminal', label: `Encerrar outros (só ${foco} continua)…`, run: () => encerrarOutros(foco) });
    todos.filter(n => !visiveis.includes(n)).forEach(n => out.push({ kind: 'Terminal', label: `Reabrir ${n}`, run: () => selecionar(a.m.nome, a.p.nome, n) }));
    a.p.terminais.forEach(t => out.push({ kind: 'Terminal', label: `Encerrar ${t.nome}`, run: () => encerrar(a, t.nome) }));
  }
  return out.filter(i => i.label.toLowerCase().includes(q));
}
function modalPal() {
  const lista = h('div', { class: 'results', role: 'listbox' });
  const pintar = () => {
    const itens = itensPaleta(); S.palSel = Math.max(0, Math.min(S.palSel, itens.length - 1));
    lista.replaceChildren(...itens.map((r, i) => h('button', { class: 'result' + (i === S.palSel ? ' on' : ''), role: 'option', onclick: () => r.run() }, h('span', { class: 'kind' }, r.kind), h('span', { style: { flex: 1 } }, r.label))));
    return itens;
  };
  pintar();
  const input = h('input', { 'data-autofocus': '', value: S.consulta, placeholder: 'Buscar projetos e ações…', 'aria-label': 'Buscar projetos e ações',
    oninput: e => { S.consulta = e.target.value; S.palSel = 0; pintar(); },
    onkeydown: e => {
      const itens = itensPaleta();
      if (e.key === 'ArrowDown') { e.preventDefault(); S.palSel = Math.min(S.palSel + 1, itens.length - 1); pintar(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); S.palSel = Math.max(S.palSel - 1, 0); pintar(); }
      else if (e.key === 'Enter' && itens[S.palSel]) { e.preventDefault(); itens[S.palSel].run(); }
    } });
  return h('div', { class: 'palette', onmousedown: e => e.stopPropagation(), role: 'dialog', 'aria-label': 'Paleta de comandos' }, input, lista);
}

/* ---------- celular: abas (Projetos, Terminais, Facilitadores, Config) ---------- */
const TECLAS = [['esc', '\x1b'], ['tab', '\t'], ['ctrl-c', '\x03'], ['↑', '\x1b[A'], ['↓', '\x1b[B'], ['clear', '\x0c']];
function focoPane() {
  const a = atual(); if (!a) return null;
  return panes.get(`${a.m.nome}/${a.p.nome}/${focoDe(a, nomesTodos(a).visiveis)}`) || null;
}
function enviarTecla(seq) { const p = focoPane(); if (p) { p.enviar(seq); p.term.focus(); } }
function irAba(aba) {
  S.mtab = aba; ls.set('mtab', aba);
  if (aba === 'fac') api('/api/ferramentas').then(l => { S.ferr = l; renderMobile(); }).catch(() => {});
  renderTudo();
}
function renderMobile(auto = false) {
  if (!$('#mob')) return;
  const a = atual(), aba = S.mtab;
  let tot = 0; maquinas().forEach(m => m.projetos.forEach(p => { tot += p.terminais.length; }));
  const aguard = renderTitulo();
  const tit = {
    proj: ['Projetos', `${maquinas().length} ${maquinas().length === 1 ? 'máquina' : 'máquinas'} · ${tot} ${tot === 1 ? 'sessão' : 'sessões'}`],
    tty: [a ? a.p.nome : 'Terminais', a ? `${a.m.nome} · ${Math.round(a.m.cpu)}% cpu · ${fm(a.m.mem_mb)}` : (S.semPainel ? 'painel inacessível' : 'escolha um projeto')],
    fac: ['Facilitadores', 'tty-add · tty-push'], cfg: ['Configurações', 'Painel e avisos'],
  }[aba];
  $('#mob-title').textContent = tit[0]; $('#mob-sub').textContent = tit[1];
  $('#mob-add').hidden = aba !== 'tty';
  ['proj', 'tty', 'fac', 'cfg'].forEach(k => { $('#mob-' + k).hidden = k !== aba; });
  $('#mob-nav').replaceChildren(...[['proj', 'Projetos'], ['tty', 'Terminais'], ['fac', 'Facilitadores'], ['cfg', 'Config']].map(([k, l]) =>
    h('button', { class: aba === k ? 'on' : '', 'aria-current': aba === k ? 'page' : null, onclick: () => irAba(k) }, l,
      k === 'tty' && aguard ? h('span', { class: 'dot s-aguardando', style: { width: '6px', height: '6px' }, title: `${aguard} aguardando você` }) : null)));
  if (aba === 'proj') renderMobProj();
  else if (aba === 'tty') renderMobTty();
  else if (aba === 'fac') {
    const pg = $('#mob-fac'), rol = pg.scrollTop;
    pg.replaceChildren(...[h('div', { class: 'sub', style: { fontSize: '13px', lineHeight: '1.5' } }, 'Instale uma ferramenta e leve a configuração dela entre as máquinas.'), ...listaFerramentas()].filter(Boolean));
    pg.scrollTop = rol;
  } else if (aba === 'cfg') {
    const pg = $('#mob-cfg');
    if (auto && pg.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;  // não rouba o foco de quem digita
    const rol = pg.scrollTop;
    pg.replaceChildren(...[...cfgGeral(), ...cfgNotif(), ...cfgMaq()].filter(Boolean));  // (replaceChildren transforma null em o texto "null")
    pg.scrollTop = rol;
  }
}
function renderMobProj() {
  const pg = $('#mob-proj'), rol = pg.scrollTop, nos = [];
  if (!maquinas().length) nos.push(h('div', { class: 'empty' }, S.semPainel ? 'Painel inacessível.' : 'Nenhuma máquina ainda. Em Config › Máquinas você gera o comando de convite.'));
  maquinas().forEach(m => {
    const memPct = m.ram_mb ? Math.min(100, (m.mem_mb / m.ram_mb) * 100) : 0;
    const sp = (t, c) => h('span', c ? { class: c } : null, t);
    nos.push(h('div', null,
      h('div', { class: 'mcard' },
        h('div', { class: 'mhead' }, h('span', { class: 'dot', style: { width: '7px', height: '7px', background: m.online ? 'var(--ok)' : 'var(--dim)' } }),
          h('span', { class: 'mname' }, m.nome), h('span', { class: 'mcores' }, m.cores ? `${m.cores} vCPU · ${Math.round(m.ram_mb / 1024)} GB` : (m.online ? '' : 'offline')),
          h('button', { class: 'mcard-add', 'aria-label': `Novo projeto em ${m.nome}`, disabled: !m.online, onclick: () => novoProjetoEm(m.nome) }, '+')),
        h('div', { class: 'mbars' },
          sp('CPU'), h('div', { class: 'bar' }, h('i', { style: { width: Math.min(100, m.cpu) + '%', background: 'var(--acc)' } })), sp(Math.round(m.cpu) + '%', 'val'),
          sp('MEM'), h('div', { class: 'bar' }, h('i', { style: { width: memPct + '%', background: 'var(--fg2)' } })), sp(fm(m.mem_mb), 'val'))),
      m.projetos.map(p => h('div', { class: 'mproj-wrap' }, h('button', { class: 'mproj', onclick: () => selecionar(m.nome, p.nome) },
        h('span', { class: 'dot s-' + p.estado, style: { width: '7px', height: '7px' } }),
        h('div', { class: 'pmain' }, h('div', { class: 'pname' }, p.nome), h('div', { class: 'pmeta' }, `${p.branch || '—'} · ${p.terminais.length} tty`)),
        h('div', { class: 'pres' }, h('div', null, pct(p.cpu)), h('div', null, fm(p.mem_mb))),
        h('span', { class: 'chev' }, '›')),
        h('button', { class: 'mproj-rm', 'aria-label': `Remover o projeto ${p.nome}`, onclick: () => removerProjeto(m, p) }, '×')))));
  });
  pg.replaceChildren(...nos); pg.scrollTop = rol;
}
function renderMobTty() {
  const ch = $('#mob-chips'), a = atual(); if (!ch) return;
  if (!a) return ch.replaceChildren();
  const { todos, visiveis } = nomesTodos(a), foco = focoDe(a, visiveis);
  ch.replaceChildren(...todos.map(n => {
    const t = a.p.terminais.find(x => x.nome === n);
    const fechada = !visiveis.includes(n);
    return h('span', { class: 'mchip' + (n === foco && !fechada ? ' on' : '') + (fechada ? ' dim' : '') },
      h('button', { class: 'mchip-go', onclick: () => { if (fechada) { if (n === 'main') reabrirMain(); else reabrirVista(n); } else { focar(n); renderTudo(); } } },
        h('span', { class: 'dot s-' + (t ? t.estado : 'offline'), style: { width: '6px', height: '6px' } }), n),
      h('button', { class: 'mchip-x', 'aria-label': `Encerrar ${n}`, onclick: e => { e.stopPropagation(); encerrarTerminal(n); } }, '×'));
  }),
    todos.length ? h('button', { class: 'mchip mchip-clear', title: 'Limpar todos e voltar ao main', 'aria-label': 'Limpar todos os terminais', onclick: limparProjetoAtual }, '🗑') : null);
}

/* ---------- dados ---------- */
function garantirSelecao() {
  if (atual()) return;
  const m = (location.hash.match(/^#\/([^/]+)\/([^/]+)(?:\/([^/]+))?/) || []);
  const alvo = m[1] && achar({ m: decodeURIComponent(m[1]), p: decodeURIComponent(m[2]) });
  if (alvo) { S.sel = { m: alvo.m.nome, p: alvo.p.nome }; if (m[3]) { S.foco[chave(alvo.m.nome, alvo.p.nome)] = decodeURIComponent(m[3]); focoPedido = `${alvo.m.nome}/${alvo.p.nome}/${decodeURIComponent(m[3])}`; } return; }
  const todos = maquinas().flatMap(mq => mq.projetos.map(p => ({ mq, p })));
  const esc = todos.find(x => x.p.estado === 'aguardando') || todos.find(x => x.p.estado === 'rodando') || todos[0];
  S.sel = esc ? { m: esc.mq.nome, p: esc.p.nome } : null;
}
function aplicarHash() {
  const m = location.hash.match(/^#\/([^/]+)\/([^/]+)(?:\/([^/]+))?/); if (!m) return;
  const a = achar({ m: decodeURIComponent(m[1]), p: decodeURIComponent(m[2]) });
  if (a) selecionar(a.m.nome, a.p.nome, m[3] && decodeURIComponent(m[3]));
}
async function atualizar() {
  try { S.est = await api('/api/estado', { cache: 'no-store' }); S.semPainel = false; }
  catch { S.semPainel = true; }
  garantirSelecao();
  renderTudo(true);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && S.modal) { fecharModal(); return; }
  const k = e.key.toLowerCase();
  const dentroTerminal = !!e.target.closest && !!e.target.closest('.xterm');
  if (k === 'k' && (e.metaKey || e.altKey || (e.ctrlKey && !dentroTerminal))) { e.preventDefault(); abrirModal('pal'); }
});
window.addEventListener('hashchange', aplicarHash);
matchMedia('(max-width:859px)').addEventListener('change', () => { panes.forEach(p => p.dispose()); panes.clear(); renderTudo(); });

esqueleto();
atualizar().then(aplicarHash);
setInterval(atualizar, 2500);
})();
