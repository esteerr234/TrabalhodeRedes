"""
Camada de rede P2P (Full Mesh TCP).

Responsabilidades:
  * manter o socket TCP de escuta do jogador antes mesmo do registro no lobby;
  * ao receber PARTIDA_INICIADA, estabelecer malha completa (todos <-> todos)
    usando a regra determinística anti-duplicação (id_jogador maior conecta
    no id_jogador menor);
  * autenticar conexões com handshake OLA / OLA_OK usando chave_partida;
  * manter heartbeat (PING/PONG) e timeout nos sockets P2P para detectar
    quedas/travamentos de pares;
  * entregar mensagens de jogo e eventos de desconexão em uma fila segura
    entre threads.
"""

import logging
import queue
import socket
import threading
import time

import protocolo as p

log = logging.getLogger("p2p")

EVENTO_MENSAGEM = "MENSAGEM"
EVENTO_PAR_DESCONECTADO = "PAR_DESCONECTADO"

TIMEOUT_HANDSHAKE = 5.0
INTERVALO_PING_PADRAO = 3.0
TIMEOUT_PAR_PADRAO = 10.0


class RedeP2P:
    """Gerencia conexões TCP diretas (full mesh) entre os pares da partida."""

    def __init__(
        self,
        host_escuta="0.0.0.0",
        porta_escuta=0,
        intervalo_ping=INTERVALO_PING_PADRAO,
        timeout_par=TIMEOUT_PAR_PADRAO,
    ):
        self.host_escuta = host_escuta
        self.porta_escuta = porta_escuta
        self.intervalo_ping = intervalo_ping
        self.timeout_par = timeout_par

        self._sock_servidor = None
        self._rodando = threading.Event()

        # Estado da partida atual
        self._lock = threading.Lock()
        self._cond_pares = threading.Condition(self._lock)
        self._partida_configurada = threading.Event()
        self._parar_partida = threading.Event()

        self._meu_id = None
        self._id_partida = None
        self._chave_partida = None
        self._esperados = {}        # id_jogador -> dict info do par
        self._pares = {}            # id_jogador -> p.Conexao
        self._desconectados = set() # ids já notificados como desconectados
        self._fila_eventos = queue.Queue()

    # ------------------------------------------------------------------ escuta
    def iniciar_servidor(self):
        """Abre o socket TCP de escuta P2P e inicia a thread de accept()."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host_escuta, self.porta_escuta))
        sock.listen()
        self._sock_servidor = sock
        self.porta_escuta = sock.getsockname()[1]
        self._rodando.set()
        threading.Thread(
            target=self._aceitar_conexoes,
            daemon=True,
            name=f"p2p-accept-{self.porta_escuta}",
        ).start()
        log.info("Escuta P2P ativa em %s:%d", self.host_escuta, self.porta_escuta)
        return self.porta_escuta

    def _aceitar_conexoes(self):
        while self._rodando.is_set():
            try:
                sock, end = self._sock_servidor.accept()
            except OSError:
                break
            threading.Thread(
                target=self._tratar_entrada,
                args=(sock,),
                daemon=True,
                name=f"p2p-in-{end[0]}:{end[1]}",
            ).start()

    # ------------------------------------------------------------- malha mesh
    def conectar_partida(self, meu_id, id_partida, chave_partida, jogadores, timeout_malha=15.0):
        """
        Estabelece a malha completa com todos os demais jogadores da partida.

        Regra anti-duplicação:
          * se meu_id > peer_id: este processo inicia a conexão TCP e envia OLA;
          * se meu_id < peer_id: este processo aguarda a conexão no socket servidor.

        Retorna (ids_conectados, ids_faltantes).
        """
        self.desconectar_partida()

        with self._lock:
            self._meu_id = meu_id
            self._id_partida = id_partida
            self._chave_partida = chave_partida
            self._esperados = {
                j["id_jogador"]: j for j in jogadores if j["id_jogador"] != meu_id
            }
            self._pares = {}
            self._desconectados = set()
            self._fila_eventos = queue.Queue()
            self._parar_partida.clear()
            self._partida_configurada.set()

        prazo = time.monotonic() + timeout_malha

        # Inicia conexões ativas apenas para pares com id_jogador menor que meu_id
        for peer_id, info in self._esperados.items():
            if meu_id > peer_id:
                threading.Thread(
                    target=self._conectar_ativo,
                    args=(info, prazo),
                    daemon=True,
                    name=f"p2p-out-{meu_id}-para-{peer_id}",
                ).start()

        # Aguarda todos os pares conectarem ou o prazo esgotar
        with self._cond_pares:
            while (
                not self._parar_partida.is_set()
                and len(self._pares) < len(self._esperados)
            ):
                restante = prazo - time.monotonic()
                if restante <= 0:
                    break
                self._cond_pares.wait(timeout=restante)
            conectados = set(self._pares.keys())
            faltantes = set(self._esperados.keys()) - conectados

        # Inicia thread de heartbeat para monitorar a saúde dos pares
        threading.Thread(
            target=self._heartbeat,
            daemon=True,
            name=f"p2p-ping-{meu_id}",
        ).start()

        log.info(
            "Malha P2P da partida %d: conectados=%s faltantes=%s",
            id_partida,
            sorted(conectados),
            sorted(faltantes),
        )
        return sorted(conectados), sorted(faltantes)

    def _conectar_ativo(self, peer_info, prazo):
        """Conecta ativamente a um par de id menor e executa o handshake OLA/OLA_OK."""
        peer_id = peer_info["id_jogador"]
        host = peer_info["host"]
        porta = peer_info["porta"]

        while not self._parar_partida.is_set() and time.monotonic() < prazo:
            sock = None
            try:
                sock = socket.create_connection((host, porta), timeout=3.0)
                sock.settimeout(TIMEOUT_HANDSHAKE)
                conexao = p.Conexao(sock)
                conexao.enviar(
                    p.OLA,
                    id_jogador=self._meu_id,
                    id_partida=self._id_partida,
                    chave_partida=self._chave_partida,
                )
                resposta = conexao.receber()
                if (
                    resposta.get("tipo") == p.OLA_OK
                    and resposta.get("id_partida") == self._id_partida
                    and resposta.get("id_jogador") == peer_id
                ):
                    sock.settimeout(self.timeout_par)
                    if self._registrar_par(peer_id, conexao):
                        self._iniciar_leitor(peer_id, conexao)
                    else:
                        conexao.fechar()
                    return
                else:
                    log.warning("Handshake rejeitado pelo par %d: %s", peer_id, resposta)
                    conexao.fechar()
                    return
            except (OSError, p.ConexaoFechada, p.ErroProtocolo) as e:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                log.debug("Tentativa de conexão ao par %d (%s:%d) falhou: %s", peer_id, host, porta, e)
                time.sleep(0.2)

    def _tratar_entrada(self, sock):
        """Valida o handshake OLA de uma conexão recebida no socket de escuta."""
        sock.settimeout(TIMEOUT_HANDSHAKE)
        conexao = p.Conexao(sock)
        try:
            msg = conexao.receber()
            if msg.get("tipo") != p.OLA:
                conexao.enviar_erro(p.E_ESTADO_INVALIDO, "esperava OLA no handshake")
                conexao.fechar()
                return

            # Aguarda brevemente caso o outro par tenha recebido PARTIDA_INICIADA antes de nós
            if not self._partida_configurada.wait(timeout=TIMEOUT_HANDSHAKE):
                conexao.enviar_erro(p.E_ESTADO_INVALIDO, "par ainda não está em partida")
                conexao.fechar()
                return

            id_partida = msg.get("id_partida")
            chave = msg.get("chave_partida")
            peer_id = msg.get("id_jogador")

            with self._lock:
                valido_partida = (
                    id_partida == self._id_partida
                    and chave == self._chave_partida
                )
                valido_peer = (
                    isinstance(peer_id, int)
                    and not isinstance(peer_id, bool)
                    and peer_id in self._esperados
                    and peer_id > self._meu_id
                    and peer_id not in self._pares
                )
                meu_id = self._meu_id

            if not valido_partida:
                conexao.enviar_erro(p.E_CHAVE_INVALIDA, "id_partida ou chave_partida inválidos")
                conexao.fechar()
                return

            if not valido_peer:
                conexao.enviar_erro(p.E_NAO_AUTORIZADO, "par inesperado ou duplicado")
                conexao.fechar()
                return

            conexao.enviar(p.OLA_OK, id_jogador=meu_id, id_partida=id_partida)
            sock.settimeout(self.timeout_par)
            if self._registrar_par(peer_id, conexao):
                self._iniciar_leitor(peer_id, conexao)
            else:
                conexao.fechar()
        except (socket.timeout, p.ConexaoFechada, p.ErroProtocolo, OSError) as e:
            log.debug("Falha no handshake de entrada %s: %s", conexao.endereco, e)
            conexao.fechar()

    def _registrar_par(self, peer_id, conexao):
        with self._cond_pares:
            if self._parar_partida.is_set() or peer_id in self._pares:
                return False
            self._pares[peer_id] = conexao
            self._cond_pares.notify_all()
            log.info("Par %d conectado (%s:%d)", peer_id, *conexao.endereco)
            return True

    def _iniciar_leitor(self, peer_id, conexao):
        threading.Thread(
            target=self._ler_par,
            args=(peer_id, conexao),
            daemon=True,
            name=f"p2p-ler-{self._meu_id}-de-{peer_id}",
        ).start()

    # --------------------------------------------------------- leitura e ping
    def _ler_par(self, peer_id, conexao):
        motivo = "conexão encerrada"
        try:
            while not self._parar_partida.is_set():
                try:
                    msg = conexao.receber()
                except p.ErroProtocolo as e:
                    conexao.enviar_erro(e.codigo, e.descricao)
                    continue

                tipo = msg["tipo"]
                if tipo == p.PING:
                    try:
                        conexao.enviar(p.PONG)
                    except p.ConexaoFechada:
                        break
                    continue
                if tipo == p.PONG:
                    continue
                if tipo == p.TCHAU:
                    motivo = "saiu voluntariamente (TCHAU)"
                    break

                self._fila_eventos.put({
                    "evento": EVENTO_MENSAGEM,
                    "remetente": peer_id,
                    "msg": msg,
                })
        except socket.timeout:
            motivo = f"timeout ({self.timeout_par:.0f}s sem resposta)"
        except p.ConexaoFechada:
            motivo = "conexão perdida"
        finally:
            self._registrar_desconexao(peer_id, conexao, motivo)

    def _heartbeat(self):
        while not self._parar_partida.wait(self.intervalo_ping):
            with self._lock:
                snapshot = list(self._pares.items())
            for peer_id, conexao in snapshot:
                try:
                    conexao.enviar(p.PING)
                except p.ConexaoFechada:
                    self._registrar_desconexao(peer_id, conexao, "falha ao enviar PING")

    def _registrar_desconexao(self, peer_id, conexao, motivo):
        conexao.fechar()
        notificar = False
        with self._lock:
            if self._pares.get(peer_id) is conexao:
                del self._pares[peer_id]
            if not self._parar_partida.is_set() and peer_id not in self._desconectados:
                self._desconectados.add(peer_id)
                notificar = True
        if notificar:
            log.warning("Par %d desconectado: %s", peer_id, motivo)
            self._fila_eventos.put({
                "evento": EVENTO_PAR_DESCONECTADO,
                "id_jogador": peer_id,
                "motivo": motivo,
            })

    # ------------------------------------------------------------ envio / api
    def broadcast(self, tipo, **campos):
        """Envia uma mensagem para todos os pares conectados."""
        with self._lock:
            snapshot = list(self._pares.items())
        for peer_id, conexao in snapshot:
            try:
                conexao.enviar(tipo, **campos)
            except p.ConexaoFechada:
                self._registrar_desconexao(peer_id, conexao, "falha ao enviar broadcast")

    def enviar_para(self, peer_id, tipo, **campos):
        """Envia uma mensagem para um par específico."""
        with self._lock:
            conexao = self._pares.get(peer_id)
        if conexao is None:
            return False
        try:
            conexao.enviar(tipo, **campos)
            return True
        except p.ConexaoFechada:
            self._registrar_desconexao(peer_id, conexao, "falha ao enviar mensagem")
            return False

    def proximo_evento(self, timeout=None):
        """Retira o próximo evento da fila P2P (ou None se o timeout esgotar)."""
        try:
            return self._fila_eventos.get(timeout=timeout)
        except queue.Empty:
            return None

    def pares_conectados(self):
        with self._lock:
            return sorted(self._pares.keys())

    def desconectar_partida(self):
        """Encerra todas as conexões P2P da partida atual (envia TCHAU antes)."""
        self._parar_partida.set()
        self._partida_configurada.clear()
        with self._cond_pares:
            conexoes = list(self._pares.values())
            self._pares.clear()
            self._cond_pares.notify_all()
        for conexao in conexoes:
            try:
                conexao.enviar(p.TCHAU)
            except p.ConexaoFechada:
                pass
            conexao.fechar()

    def fechar(self):
        """Encerra a partida atual e fecha o socket TCP de escuta."""
        self.desconectar_partida()
        self._rodando.clear()
        if self._sock_servidor is not None:
            try:
                self._sock_servidor.close()
            except OSError:
                pass
            self._sock_servidor = None
