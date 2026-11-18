"""
Cliente do servidor de lobby (lado do jogador).

Esconde o protocolo do lobby atrás de métodos simples:

    lobby = ClienteLobby("192.168.0.10", 5000)
    meu_id = lobby.registrar("Ana", porta_p2p=6001)
    lobby.criar_partida("Mesa 1")
    evento = lobby.proximo_evento(timeout=1)   # ATUALIZACAO_PARTIDA / PARTIDA_INICIADA

As respostas às requisições são devolvidas pelos próprios métodos; as
notificações assíncronas do lobby vão para uma fila (proximo_evento), para
que o programa do jogador as processe na sua própria thread.
"""

import queue      # filas seguras entre threads (uma thread coloca, outra tira)
import socket
import threading

import protocolo as p

TIMEOUT_RESPOSTA = 5.0  # se o lobby não responder um pedido em 5 s, desistimos (erro TIMEOUT)
INTERVALO_PING = 20.0  # menor que o TIMEOUT_CLIENTE do lobby (60 s)

# Evento local (não trafega na rede) colocado na fila quando o lobby cai.
# Serve para o jogo saber que perdeu o lobby (se a partida já começou, pode ignorar).
LOBBY_DESCONECTADO = "LOBBY_DESCONECTADO"


class ErroLobby(Exception):
    """O lobby respondeu ERRO, não respondeu ou caiu."""
    # Mesma ideia do ErroProtocolo: guarda o código e a descrição para quem capturar.

    def __init__(self, codigo, descricao=""):
        super().__init__(f"{codigo}: {descricao}")
        self.codigo = codigo
        self.descricao = descricao


class ClienteLobby:
    # Como funciona por dentro (3 threads):
    #   * thread do jogo (quem chama registrar(), criar_partida()...): envia o pedido
    #     e fica esperando a resposta na fila _respostas;
    #   * thread "lobby-leitor" (_ler): a ÚNICA que lê do socket; separa o que chega
    #     em respostas (vão para _respostas) e notificações (vão para _eventos);
    #   * thread "lobby-ping" (_heartbeat): manda PING de tempos em tempos para o
    #     lobby saber que ainda estamos vivos.
    # Precisamos do leitor separado porque o lobby pode mandar uma notificação
    # (ex.: PARTIDA_INICIADA) a qualquer momento, até no meio de um pedido nosso.

    def __init__(self, host, porta, timeout_conexao=5.0):
        # Abre a conexão TCP com o lobby (desiste se não conectar em 5 s).
        sock = socket.create_connection((host, porta), timeout=timeout_conexao)
        sock.settimeout(None)  # depois de conectado, recv() pode esperar para sempre (o leitor fica bloqueado nele)
        self._conexao = p.Conexao(sock)
        self._respostas = queue.Queue()  # respostas aos nossos pedidos (REGISTRADO, ERRO...)
        self._eventos = queue.Queue()    # notificações espontâneas (ATUALIZACAO_PARTIDA, PARTIDA_INICIADA)
        self._lock_requisicao = threading.Lock()  # uma requisição por vez
        self._encerrado = threading.Event()  # "bandeira" ligada quando a conexão acaba
        threading.Thread(target=self._ler, daemon=True, name="lobby-leitor").start()
        threading.Thread(target=self._heartbeat, daemon=True, name="lobby-ping").start()

    # -------------------------------------------------------------- requisições
    # Cada método abaixo = 1 mensagem do protocolo. Todos funcionam igual:
    # mandam o pedido, esperam a resposta certa e devolvem o campo que interessa.
    # Se o lobby responder ERRO, lançam ErroLobby.

    def registrar(self, nome, porta_p2p, host_p2p=None):
        # porta_p2p = porta onde ESTE jogador vai esperar as conexões dos outros pares.
        campos = {"nome": nome, "porta_p2p": porta_p2p}
        if host_p2p:
            campos["host_p2p"] = host_p2p  # opcional: se não mandar, o lobby usa nosso IP
        return self._requisitar(p.REGISTRAR, p.REGISTRADO, **campos)["id_jogador"]

    def listar_partidas(self):
        return self._requisitar(p.LISTAR_PARTIDAS, p.LISTA_PARTIDAS)["partidas"]

    def criar_partida(self, nome_partida, max_jogadores=p.MAX_JOGADORES):
        return self._requisitar(p.CRIAR_PARTIDA, p.PARTIDA_CRIADA,
                                nome_partida=nome_partida,
                                max_jogadores=max_jogadores)["partida"]

    def entrar_partida(self, id_partida):
        return self._requisitar(p.ENTRAR_PARTIDA, p.ENTROU_PARTIDA,
                                id_partida=id_partida)["partida"]

    def sair_partida(self):
        self._requisitar(p.SAIR_PARTIDA, p.SAIU_PARTIDA)

    def iniciar_partida(self):
        """Só o criador pode chamar. A lista de pares chega como PARTIDA_INICIADA."""
        # A resposta (INICIO_ACEITO) só confirma; os endereços dos pares chegam
        # depois pela fila de eventos, igual para todos os membros.
        return self._requisitar(p.INICIAR_PARTIDA, p.INICIO_ACEITO)

    # --------------------------------------------------------------- eventos
    def proximo_evento(self, timeout=None):
        """Próxima notificação do lobby (dict) ou None se o tempo esgotar."""
        # timeout=None espera para sempre; timeout=1 espera no máximo 1 s.
        try:
            return self._eventos.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def conectado(self):
        # Uso: if lobby.conectado: ...  (sem parênteses, por causa do @property)
        return not self._encerrado.is_set()

    def fechar(self):
        self._encerrado.set()     # avisa o heartbeat para parar
        self._conexao.fechar()    # fecha o socket (o leitor acorda e termina)

    # ------------------------------------------------------------- internos
    def _requisitar(self, tipo, tipo_resposta, **campos):
        """Envia um pedido e espera a resposta. Base de todos os métodos acima."""
        # Lock: se duas threads pedissem ao mesmo tempo, uma poderia pegar a
        # resposta da outra. Com o lock, só existe um pedido "em voo" por vez.
        with self._lock_requisicao:
            if self._encerrado.is_set():
                raise ErroLobby(LOBBY_DESCONECTADO, "sem conexão com o lobby")
            # Descarta respostas atrasadas de uma requisição anterior que expirou.
            while not self._respostas.empty():
                self._respostas.get_nowait()
            try:
                self._conexao.enviar(tipo, **campos)
                # Espera o leitor colocar a resposta na fila (no máximo 5 s).
                resposta = self._respostas.get(timeout=TIMEOUT_RESPOSTA)
            except p.ConexaoFechada:
                raise ErroLobby(LOBBY_DESCONECTADO, "conexão com o lobby perdida")
            except queue.Empty:
                raise ErroLobby("TIMEOUT", f"lobby não respondeu a {tipo}")
        if resposta is None:
            # None é o "aviso" que o leitor coloca na fila quando a conexão cai.
            raise ErroLobby(LOBBY_DESCONECTADO, "conexão com o lobby perdida")
        if resposta["tipo"] == p.ERRO:
            # O lobby recusou o pedido (ex.: PARTIDA_CHEIA): vira exceção para quem chamou.
            raise ErroLobby(resposta.get("codigo", "?"), resposta.get("descricao", ""))
        if resposta["tipo"] != tipo_resposta:
            # Chegou uma resposta que não combina com o pedido: algo está fora do protocolo.
            raise ErroLobby(p.E_TIPO_DESCONHECIDO,
                            f"esperava {tipo_resposta}, recebeu {resposta['tipo']}")
        return resposta

    def _ler(self):
        """Thread leitora: a única que chama receber(); separa respostas de notificações."""
        try:
            while True:
                try:
                    msg = self._conexao.receber()  # bloqueia até chegar uma mensagem inteira
                except p.ErroProtocolo:
                    continue  # linha inválida vinda do lobby: ignora
                if msg["tipo"] == p.PONG:
                    continue  # resposta ao nosso PING: só confirma que o lobby está vivo
                if msg["tipo"] in p.NOTIFICACOES_LOBBY:
                    self._eventos.put(msg)     # notificação espontânea -> fila de eventos
                else:
                    self._respostas.put(msg)   # resposta a um pedido -> fila de respostas
        except p.ConexaoFechada:
            pass  # lobby caiu ou nós fechamos: sai do laço
        finally:
            self._encerrado.set()
            self._respostas.put(None)  # destrava quem espera resposta
            self._eventos.put({"tipo": LOBBY_DESCONECTADO})  # avisa o jogo que o lobby caiu

    def _heartbeat(self):
        # wait() devolve True assim que o cliente é encerrado, saindo do laço.
        # Enquanto não for encerrado, ele "dorme" 20 s e devolve False -> manda PING.
        while not self._encerrado.wait(INTERVALO_PING):
            try:
                self._conexao.enviar(p.PING)
            except p.ConexaoFechada:
                return
