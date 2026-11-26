BIN=opencode
DESC="Agente de código no terminal e suas preferências."
ITENS=(.config/opencode/opencode.jsonc .config/opencode/cli.json .config/opencode/skills .config/opencode/package.json .config/opencode/plugins)
EXCLUIR=(.config/opencode/plugins/tty-notify.js)   # gerado por `pos` conforme a versão do OpenCode de cada máquina
instalar() { curl -fsSL https://opencode.ai/install | bash; }
plugins() {
  # plugins npm do OpenCode: dependências do package.json (o código vem do registro, não do painel)
  local d="$HOME/.config/opencode"
  [ -f "$d/package.json" ] || return 0
  if command -v bun >/dev/null 2>&1; then (cd "$d" && bun install </dev/null >/dev/null 2>&1) && echo "• plugins do OpenCode: dependências instaladas (bun)" || echo "• plugins do OpenCode: bun install FALHOU"
  elif command -v npm >/dev/null 2>&1; then (cd "$d" && npm install --no-audit --no-fund </dev/null >/dev/null 2>&1) && echo "• plugins do OpenCode: dependências instaladas (npm)" || echo "• plugins do OpenCode: npm install FALHOU"
  else echo "• plugins do OpenCode: sem bun/npm, dependências não instaladas"; fi
}
pos() {
  local d="$HOME/.config/opencode/plugins" ver maior
  mkdir -p "$d"
  # o formato de plugin mudou na 2.x (export default {id, setup} e eventos session.execution.*); sem versão legível, vale o v1
  ver=$(opencode --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1) || true
  maior=${ver%%.*}
  if [ "${maior:-1}" -ge 2 ] 2>/dev/null; then
    # v2: avisa em session.execution.succeeded (terminou), .failed/session.error (erro), permission.* (aprovação) e question.* (pergunta)
    cat > "$d/tty-notify.js" <<'JS'
import { spawn, execFile } from "node:child_process"
import { mkdirSync, writeFileSync } from "node:fs"
// cada pasta carrega uma instância do plugin e todas recebem o fluxo inteiro: só a primeira a ver o id avisa
const primeiro = (id) => { try { mkdirSync("/tmp/tty-notify-ids", { recursive: true }); writeFileSync("/tmp/tty-notify-ids/" + id, "", { flag: "wx" }); return true } catch (e) { return e?.code !== "EEXIST" } }
// eventos session.execution.* não trazem location, só sessionID: a pasta da sessão está no banco do OpenCode
const pastaDaSessao = (id) => new Promise((ok) => {
  if (!/^ses_[A-Za-z0-9]+$/.test(id || "")) return ok(undefined)
  const db = (process.env.XDG_DATA_HOME || (process.env.HOME || "") + "/.local/share") + "/opencode/opencode.db"
  try {
    execFile("sqlite3", ["-readonly", db, "select directory from session_v2 where id='" + id + "'"], { timeout: 2000 }, (e, out) => ok(e ? undefined : (out || "").trim() || undefined))
  } catch { ok(undefined) }
})
export default {
  id: "tty-notify",
  setup(ctx) {
    const dir = ctx?.location?.directory || ctx?.directory || process.cwd()
    // o processo do OpenCode é um só para todas as pastas e herda o TMUX de onde foi aberto: sem isso o aviso cai no projeto dessa sessão
    const ambiente = (d) => { const e = { ...process.env, TTY_DIR: d }; delete e.TMUX; delete e.TMUX_PANE; delete e.TTY_SESSAO; return e }
    const avisar = (motivo, texto, pasta) => {
      try { spawn("tty-notify", [motivo, texto], { stdio: "ignore", detached: true, cwd: pasta || dir, env: ambiente(pasta || dir) }).on("error", () => {}).unref() } catch {}
    }
    const classificar = (tipo, event) => {
      if (/^form\.(created|asked)$/.test(tipo)) return event?.data?.form?.metadata?.kind === "question" ? ["pergunta", "OpenCode fez uma pergunta"] : ["aprovacao", "OpenCode pediu aprovação"]   // na 2.0.x o pedido de aprovação chega como form.created
      if (tipo === "session.execution.succeeded" || tipo === "session.idle") return ["terminou", "OpenCode terminou"]
      if (tipo === "session.execution.failed" || tipo === "session.error") return ["erro", "OpenCode: erro na sessão"]
      if (/^(session\.)?permission\.(asked|requested|created|updated)$/.test(tipo)) return ["aprovacao", "OpenCode pediu permissão"]
      if (/^(session\.)?question\.(asked|requested|created)$/.test(tipo)) return ["pergunta", "OpenCode fez uma pergunta"]
      return null
    }
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          const c = classificar(event.type, event)
          if (c && (!event.id || primeiro(event.id))) avisar(c[0], c[1], event.location?.directory || await pastaDaSessao(event.data?.sessionID || event.data?.form?.sessionID))
        }
      } catch {}
    })()
    return () => controller.abort()
  },
}
JS
    echo "• plugin de notificação do OpenCode instalado (formato v2, OpenCode $ver)"
  else
    # v1: avisa em session.idle (terminou), permission.* (aprovação) e session.error
    cat > "$d/tty-notify.js" <<'JS'
import { spawn } from "node:child_process"
export const TtyNotify = async (ctx) => {
  const dir = (ctx && ctx.directory) || process.cwd()
  const avisar = (motivo, texto) => {
    try { spawn("tty-notify", [motivo, texto], { stdio: "ignore", detached: true, env: { ...process.env, TTY_DIR: dir } }).on("error", () => {}).unref() } catch {}
  }
  return {
    event: async ({ event }) => {
      if (event.type === "session.idle") avisar("terminou", "OpenCode terminou")
      else if (event.type === "permission.asked" || event.type === "permission.updated")
        avisar("aprovacao", "OpenCode pediu permissão")
      else if (event.type === "session.error") avisar("erro", "OpenCode: erro na sessão")
    },
  }
}
JS
    echo "• plugin de notificação do OpenCode instalado (formato v1${ver:+, OpenCode $ver})"
  fi
}
