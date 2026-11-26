# Segurança — arquitetura de acesso

Resumo do modelo de segurança do Tessera, revisado em auditorias (incluindo 5
rodadas adversariais).
Placeholders apenas: `painel.example`, `203.0.113.10`.

## Modelo

```
humano ──https://painel.example──▶ login (senha + TOTP) ──▶ painel ──(ação validada)──▶ agentes
                                                              │
                                  tailnet (canal interno) ────┘──▶ ttyd das máquinas
agentes ──https──▶ painel   (agentes só devolvem dados ao painel)
```

- **Única entrada humana:** `https://<PAINEL_DOMINIO>`. Qualquer outro Host
  (IP, localhost, outro nome) recebe 421. Conexões vindas da tailnet ao painel
  são descartadas em silêncio.
- **Tailnet** é só o canal painel → ttyd. O firewall (`projetos/tty-firewall`,
  chain `TESSERA-TTYD`) deixa **apenas o IP do painel** (`--painel-ip`) alcançar
  as portas do ttyd; sem loopback; falha fechada sem root/iptables.
- **Terminal só pelo relé** `/ws` do painel. O ttyd exige `-c tessera:<CRED>` e
  o `AuthToken` no primeiro frame; o painel reescreve esse frame, então o CRED
  nunca chega ao navegador.
- **Aceito:** quem entrar logado no painel executa comandos nos terminais
  (podem ser root). Por isso toda a defesa está em impedir o login.

## Login e sessão

| Item | Regra |
|---|---|
| Credenciais | `PAINEL_SENHA` (≥ 20) + código TOTP de 6 dígitos (RFC 6238, ±1 passo) |
| Replay | cada código vale uma vez; persistido em `dados/totp_ultimo` |
| Limite por IP | mais de 5 falhas de login em 60 s → 429 + `Retry-After`; a reincidência dobra o bloqueio (60 s até 1 h) e o login certo zera |
| Força bruta no 2FA | 5 erros de código com senha certa em 5 min → pausa uniforme (429) + alerta Telegram |
| Validade | 30 min; renova só por atividade do usuário (`POST /api/sessao/renovar`, no máx. 1/min). Polling não renova |
| Teto | 8 h absolutas |
| Expirou | F5, terminal novo, reconexão e ações novas pedem login; terminal já aberto continua |
| Cookie | `__Host-Tessera`, `SameSite=Strict`, `Secure`, atrelado ao User-Agent |
| Logout | encerra a sessão e derruba os relés dela |
| CSRF/CSWSH | checagem de `Origin` (esquema + host) |

## Tokens de agente

`<nome>.<HMAC-SHA256(PAINEL_TOKEN, nome)>`: um por máquina, escopo restrito ao
plano-agente e `GET /api/config`, preso à identidade da máquina. Sem token = 401.
Um token vazado não dá acesso ao painel humano.

## Configuração de produção

O painel recusa subir (e diz o que falta) sem:

| Variável | Requisito |
|---|---|
| `PAINEL_DOMINIO` | domínio único servido |
| `PAINEL_TLS_CERT` / `PAINEL_TLS_KEY` | certificado e chave |
| `PAINEL_ATRAS_PROXY=1` | (opcional) entrada só por proxy local (Caddy): exige `PAINEL_BIND=127.0.0.1`, aceita só loopback com `X-Forwarded-For`; o proxy deve manter o `Host` original (`header_up Host {host}`) |
| `PAINEL_TOTP_SECRET` | gerar com `python3 painel/painel.py --totp-novo` (mostra QR) |
| `PAINEL_SENHA` | ≥ 20 caracteres |
| `PAINEL_TOKEN` | ≥ 32 caracteres |

Máquinas: `bootstrap.sh --painel https://<dominio> --painel-ip <IP tailnet do painel>`.
`tty-push` exige `TTY_SENHA` e `TTY_TOTP`. `PAINEL_LOCALHOST_LIVRE=1` é só para dev.

## Fora do código (responsabilidade do operador)

- Firewall/NSG: expor só 443 (e 22 restrito); nada de portas de ttyd na internet.
- DNS e certificado do domínio; renovação do cert.
- Rotacionar segredos antigos; recriar convites (tokens antigos não valem).
- `hidepid=2` no host: o `CRED` do ttyd aparece no `ps` do servidor.
- Validar com scan externo após subir.

## Riscos residuais aceitos

- CRED no argv do ttyd (modo root: firewall fecha o loopback; sem root: use `hidepid=2`).
- Chute de senha limitado só por IP (a senha longa é a proteção).
- Contador de erros do 2FA fica em memória (zera ao reiniciar).
- Um token pode ocupar os 16 slots de agentes da própria máquina.
- DoS volumétrico distribuído (precisa de CDN/proxy).

## Verificação

`./test.sh` (painel + agente + `testes/shell.sh`). As regressões de cada achado das
auditorias vivem em `painel/test_seguranca*.py` e `agente/test_seguranca2.py`.
