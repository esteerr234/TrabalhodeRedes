"""
Camada do protocolo de aplicação.

Servidor de lobby, pelo cliente do lobby e pela comunicação entre
pares. Define:

  * o enquadramento das mensagens sobre TCP;
  * os tipos de mensagem e os códigos de erro;
  * funções de validação de campos.

Formato no fio
--------------
Cada mensagem é UM objeto JSON codificado em UTF-8 e terminado por '\n'
. O objeto sempre contém o campo "tipo" (definido como uma string).
O JSON gerado nunca contém '\n' "cru", então o '\n' delimita a mensagem.
Mensagens maiores que TAM_MAX_MENSAGEM são rejeitadas.

Exemplo:  {"tipo":"REGISTRAR","nome":"Ana","porta_p2p":6001}\n
"""

import json
import socket
import threading

VERSAO_PROTOCOLO = 1
CODIFICACAO = "utf-8"
TAM_MAX_MENSAGEM = 64 * 1024  # 64 KiB por mensagem

# --------------------------------------------------------------------------
# Tipos de mensagem: Jogador <-> Lobby
# --------------------------------------------------------------------------
# Requisições (jogador -> lobby)
REGISTRAR = "REGISTRAR"
LISTAR_PARTIDAS = "LISTAR_PARTIDAS"
CRIAR_PARTIDA = "CRIAR_PARTIDA"
ENTRAR_PARTIDA = "ENTRAR_PARTIDA"
INICIAR_PARTIDA = "INICIAR_PARTIDA"
SAIR_PARTIDA = "SAIR_PARTIDA"

# Respostas (lobby -> jogador, cada requisição separada individualmente)
REGISTRADO = "REGISTRADO"
LISTA_PARTIDAS = "LISTA_PARTIDAS"
PARTIDA_CRIADA = "PARTIDA_CRIADA"
ENTROU_PARTIDA = "ENTROU_PARTIDA"
INICIO_ACEITO = "INICIO_ACEITO"
SAIU_PARTIDA = "SAIU_PARTIDA"

# Notificações (lobby -> jogador, sem requisição correspondente)
ATUALIZACAO_PARTIDA = "ATUALIZACAO_PARTIDA"
PARTIDA_INICIADA = "PARTIDA_INICIADA"
NOTIFICACOES_LOBBY = {ATUALIZACAO_PARTIDA, PARTIDA_INICIADA}

# --------------------------------------------------------------------------
# Tipos de mensagem: Par <-> Par (camada de conexão; o jogo define os seus)
# --------------------------------------------------------------------------
OLA = "OLA"          # handshake enviado por quem abre a conexão
OLA_OK = "OLA_OK"    # handshake aceito
TCHAU = "TCHAU"      # saída voluntária da partida
TIPOS_RESERVADOS_P2P = {OLA, OLA_OK, TCHAU}

# Tipos de mensagem: Par <-> Par (camada de jogo)
COMMIT_SEMENTE = "COMMIT_SEMENTE"            # fase 1 do Commit-Reveal (SHA-256)
REVELAR_SEMENTE = "REVELAR_SEMENTE"          # fase 2 do Commit-Reveal (semente)
ACAO_JOGAR = "ACAO_JOGAR"                    # joga uma carta da mão
ACAO_COMPRAR = "ACAO_COMPRAR"                # compra 1 carta (sem cartas válidas)
ACAO_JOGAR_COMPRADA = "ACAO_JOGAR_COMPRADA"  # joga imediatamente a carta comprada
ACAO_PASSAR = "ACAO_PASSAR"                  # passa a vez após comprar carta jogável
TIPOS_JOGO_P2P = {
    COMMIT_SEMENTE,
    REVELAR_SEMENTE,
    ACAO_JOGAR,
    ACAO_COMPRAR,
    ACAO_JOGAR_COMPRADA,
    ACAO_PASSAR,
}

# --------------------------------------------------------------------------
# Comuns aos dois canais
# --------------------------------------------------------------------------
PING = "PING"
PONG = "PONG"
ERRO = "ERRO"

# Códigos de erro (tentativas de identificar erros durante execução do protocolo)
E_JSON_INVALIDO = "JSON_INVALIDO"
E_MENSAGEM_GRANDE = "MENSAGEM_MUITO_GRANDE"
E_TIPO_DESCONHECIDO = "TIPO_DESCONHECIDO"
E_CAMPO_INVALIDO = "CAMPO_INVALIDO"
E_ESTADO_INVALIDO = "ESTADO_INVALIDO"
E_PARTIDA_INEXISTENTE = "PARTIDA_INEXISTENTE"
E_PARTIDA_CHEIA = "PARTIDA_CHEIA"
E_PARTIDA_EM_ANDAMENTO = "PARTIDA_EM_ANDAMENTO"
E_NAO_AUTORIZADO = "NAO_AUTORIZADO"
E_JOGADORES_INSUFICIENTES = "JOGADORES_INSUFICIENTES"
E_NOME_EM_USO = "NOME_EM_USO"
E_CHAVE_INVALIDA = "CHAVE_INVALIDA"
E_FORA_DE_TURNO = "FORA_DE_TURNO"
E_JOGADA_INVALIDA = "JOGADA_INVALIDA"
E_SEMENTE_INVALIDA = "SEMENTE_INVALIDA"

# Regras de tamanho de partida (min 3 e max 4)
MIN_JOGADORES = 3
MAX_JOGADORES = 4


class ErroProtocolo(Exception):
    """Mensagem malformada ou que viola o protocolo."""

    def __init__(self, codigo, descricao=""):
        super().__init__(f"{codigo}: {descricao}") #se der erro mostra essa pedrada aq
        self.codigo = codigo #guarda o codigo do erro
        self.descricao = descricao # guarda a explicacao do erro


class ConexaoFechada(Exception):
    """O outro lado encerrou a conexão."""
    # ponta oposta á de quem tá executando o código, ou seja, o outro lado fechou a conexão!
    #Servidor -> jogador conecta
    #ClienteLobby-> servidor conecta


def codificar(tipo, **campos):
    """Monta(em bytes) de uma mensagem pronta pra enviar"""
    msg = {"tipo": tipo, **campos}
    dados = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode(CODIFICACAO)
    if len(dados) + 1 > TAM_MAX_MENSAGEM:
        raise ErroProtocolo(E_MENSAGEM_GRANDE, f"{len(dados)} bytes")
    return dados + b"\n"


def decodificar(linha):
    """Converte uma linha (bytes, sem o '\\n') em dict. Lança ErroProtocolo."""
    """(nao sei exatamente o que faz :) )"""
    try:
        msg = json.loads(linha.decode(CODIFICACAO))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ErroProtocolo(E_JSON_INVALIDO, str(e))
    if not isinstance(msg, dict):
        raise ErroProtocolo(E_JSON_INVALIDO, "a mensagem deve ser um objeto JSON")
    if not isinstance(msg.get("tipo"), str):
        raise ErroProtocolo(E_CAMPO_INVALIDO, "campo 'tipo' ausente ou não é string")
    return msg


class Conexao:
    """
    Envolve um socket TCP e entrega mensagens inteiras.

    TCP é um fluxo de bytes: um recv() pode trazer meia mensagem ou várias
    mensagens juntas. Esta classe guarda um buffer e só devolve mensagens
    completas. O envio é protegido por lock porque várias threads (ex.: a
    thread de heartbeat e a do jogo) podem enviar pelo mesmo socket.
    """
    #em resumo, junta a mensagem o remontando pra evitar receber em pedaços, antes de enregar ao jogo em si.

    def __init__(self, sock):
        self.sock = sock # tcp já conectado, socket já conectado, ou seja, já tem o ip e porta do outro lado. 
        #veio de uma função lá do clientelobby se pá, conferir.
        try:
            self.endereco = sock.getpeername() # ip da outra ponta de conexão, endereço do outro lado da conexão
        except OSError:
            self.endereco = ("?", 0) # endereço desconhecido
        self._buffer = b""
        self._lock_envio = threading.Lock() #envio de uma thread por vez, não misturar as mensagens
        self._fechada = False #so e fechada se o outro lado fechou a conexão, ou seja, se o outro lado fechou a conexão, não pode enviar mais nada, só receber.

    def enviar(self, tipo, **campos):
        dados = codificar(tipo, **campos)
        with self._lock_envio:
            if self._fechada:
                raise ConexaoFechada()
            try:
                self.sock.sendall(dados)
            except OSError as e:
                raise ConexaoFechada() from e

    def enviar_erro(self, codigo, descricao="", ref_tipo=None):
        """Envia ERRO sem propagar falha (o outro lado pode já ter saído)."""
        try:
            self.enviar(ERRO, codigo=codigo, descricao=descricao, ref_tipo=ref_tipo)
        except ConexaoFechada:
            pass

    def receber(self):
        """
        Bloqueia até chegar uma mensagem completa e a devolve como dict.

        Lança ConexaoFechada se o outro lado fechou, ErroProtocolo se a linha
        recebida é inválida (a linha é descartada e a conexão continua
        utilizável) e socket.timeout se um timeout foi configurado no socket.
        """
        while b"\n" not in self._buffer:
            if len(self._buffer) > TAM_MAX_MENSAGEM:
                self._buffer = b""
                raise ErroProtocolo(E_MENSAGEM_GRANDE, "linha excede o limite")
            try:
                pedaco = self.sock.recv(4096)
            except socket.timeout:
                raise
            except OSError as e:
                raise ConexaoFechada() from e
            if not pedaco:
                raise ConexaoFechada()
            self._buffer += pedaco
        linha, self._buffer = self._buffer.split(b"\n", 1)
        # O limite inclui o delimitador, como em codificar(). Verifica a
        # linha completa antes de decodificar, preservando as próximas linhas.
        if len(linha) + 1 > TAM_MAX_MENSAGEM:
            raise ErroProtocolo(E_MENSAGEM_GRANDE, "linha excede o limite")
        return decodificar(linha.rstrip(b"\r"))

    def fechar(self):
        with self._lock_envio:
            if self._fechada:
                return
            self._fechada = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    @property
    def fechada(self):
        return self._fechada


# --------------------------------------------------------------------------
# Validação de campos
# --------------------------------------------------------------------------
def campo_str(msg, nome, min_len=1, max_len=32):
    valor = msg.get(nome)
    if not isinstance(valor, str) or not (min_len <= len(valor.strip()) <= max_len):
        raise ErroProtocolo(E_CAMPO_INVALIDO,
                            f"'{nome}' deve ser texto com {min_len} a {max_len} caracteres")
    return valor.strip()


def campo_int(msg, nome, minimo=None, maximo=None):
    valor = msg.get(nome)
    # bool é subclasse de int em Python; não aceitamos true/false como número
    if not isinstance(valor, int) or isinstance(valor, bool):
        raise ErroProtocolo(E_CAMPO_INVALIDO, f"'{nome}' deve ser inteiro")
    if (minimo is not None and valor < minimo) or (maximo is not None and valor > maximo):
        raise ErroProtocolo(E_CAMPO_INVALIDO, f"'{nome}' fora do intervalo [{minimo}, {maximo}]")
    return valor


def campo_carta(msg, nome="carta"):
    """Valida e converte o campo de carta ([cor, valor]) em tupla (cor, valor)."""
    valor = msg.get(nome)
    if (not isinstance(valor, (list, tuple)) or len(valor) != 2
            or not isinstance(valor[0], str) or not isinstance(valor[1], str)):
        raise ErroProtocolo(E_CAMPO_INVALIDO, f"'{nome}' deve ser uma lista [cor, valor]")
    return (valor[0], valor[1])
