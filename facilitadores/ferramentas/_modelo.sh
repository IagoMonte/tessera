# Modelo de ferramenta. Copie para ferramentas/<nome>.sh e ajuste.
# Para usar outra ferramenta (ex.: antigravity), basta um arquivo assim; o resto não muda.
BIN=nome-do-binario                 # comando que prova que está instalado
DESC="Uma linha: o que é e que config leva."   # aparece em Facilitadores no painel
ITENS=(.config/ferramenta/config)   # caminhos relativos a $HOME que tty-push/tty-add transferem
instalar() { curl -fsSL https://exemplo.com/install.sh | bash; }
pos() { :; }                        # ajustes depois da config (ex.: ligar hooks de notificação)
EXCLUIR=()                          # (opcional) itens que o tty-push NÃO leva (tar --exclude), por serem gerados em cada máquina
plugins() { :; }                    # (opcional) puxa/instala os plugins listados na config; roda após a config e em tty-atualizar
