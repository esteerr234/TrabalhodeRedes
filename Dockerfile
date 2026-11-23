# ==============================================================================
# Imagem única (Lobby + Cliente P2P) — Uno Distribuído (Redes de Computadores)
# Compatível com Podman (rootless) e Docker.
# ==============================================================================

# Imagem base totalmente qualificada (evita erro de short-name no Podman)
FROM docker.io/library/python:3.12-slim

# Evita geração de arquivos .pyc e desativa buffering de stdout/stderr
# (essencial para logs em tempo real e interação limpa no terminal)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Cria usuário e grupo de sistema sem privilégios para execução non-root
RUN groupadd --system app && \
    useradd --system --gid app --no-create-home app

# Define o diretório de trabalho da aplicação
WORKDIR /app

# Copia apenas os módulos Python necessários para execução
# (o projeto usa somente a biblioteca padrão do Python — sem pip/requirements)
COPY protocolo.py \
     baralho.py \
     regras.py \
     Jogador.py \
     servidor_lobby.py \
     cliente_lobby.py \
     rede_p2p.py \
     jogo_p2p.py \
     main.py \
     /app/

# Ajusta a propriedade de /app para o usuário sem privilégios
RUN chown -R app:app /app

# Executa o contêiner como usuário non-root (boa prática para Docker e Podman rootless)
USER app

# Porta TCP padrão do servidor de lobby (jogadores usam portas P2P configuráveis)
EXPOSE 5000

# Comando padrão: inicia o servidor de lobby.
# Para iniciar um jogador com a mesma imagem, sobrescreva o comando com:
#   python3 main.py --nome Ana --lobby-host lobby --lobby-porta 5000 --porta-p2p 6001
CMD ["python3", "servidor_lobby.py", "--host", "0.0.0.0", "--porta", "5000"]
