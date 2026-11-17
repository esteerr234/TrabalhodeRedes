"""
Servidor de lobby.

Responsabilidades (e SOMENTE estas):
  * registrar jogadores (nome + endereço onde cada um escuta conexões P2P);
  * criar, listar, entrar e sair de partidas;
  * quando a partida é iniciada, enviar a todos os membros a lista de pares
    (id, nome, host, porta) e uma chave da partida;
  * esquecer a partida em seguida.

O lobby NÃO participa do jogo: não embaralha, não valida jogadas e não guarda
estado de partida em andamento. Por isso, se ele cair, as partidas que já
começaram continuam normalmente (os pares já se conhecem).

Uso:
    python servidor_lobby.py --host 0.0.0.0 --porta 5000
"""

import argparse   # lê os parâmetros da linha de comando (--host, --porta)
import itertools  # itertools.count() gera ids 1, 2, 3, ... automaticamente
import logging    # mensagens de log com horário (melhor que print para servidor)
import secrets    # gera números aleatórios seguros (usado na chave da partida)
import socket
import threading

import protocolo as p  # nosso protocolo: Conexao, tipos de mensagem, códigos de erro

log = logging.getLogger("lobby")  # tudo que este arquivo loga aparece como [lobby]

# Se o cliente ficar este tempo sem mandar nada (nem PING), é desconectado.
# O cliente_lobby manda PING a cada 20 s, então um cliente vivo nunca chega a 60 s.
TIMEOUT_CLIENTE = 60.0
# Erros de protocolo tolerados por conexão antes de derrubá-la.
# Evita que um cliente com bug (ou malicioso) fique mandando lixo para sempre.
MAX_ERROS_POR_CONEXAO = 10

# Estados da sessão de um cliente no lobby (máquina de estados):
#   SEM_REGISTRO --REGISTRAR--> REGISTRADO --CRIAR/ENTRAR--> NA_SALA
#   NA_SALA --SAIR_PARTIDA ou partida iniciada--> REGISTRADO
SEM_REGISTRO = "SEM_REGISTRO"  # conectou, mas ainda não disse quem é
REGISTRADO = "REGISTRADO"      # já tem id, mas não está em nenhuma sala
NA_SALA = "NA_SALA"            # está dentro de uma partida esperando começar

# Estados de uma partida no lobby
# (só existe AGUARDANDO: quando a partida começa ela é apagada do lobby)
AGUARDANDO = "AGUARDANDO"


class Jogador:
    """Tudo que o lobby sabe sobre um jogador conectado."""

    def __init__(self, id_jogador, nome, host, porta, conexao):
        self.id = id_jogador      # número único dado pelo lobby (1, 2, 3...)
        self.nome = nome
        self.host = host          # IP onde ESTE jogador espera conexões dos outros pares
        self.porta = porta        # porta onde ESTE jogador espera conexões dos outros pares
        self.conexao = conexao    # Conexao TCP com o lobby (para mandar notificações a ele)
        self.id_partida = None    # None = não está em sala; número = id da sala onde está

    def como_par(self):
        # Formato enviado aos outros jogadores no PARTIDA_INICIADA:
        # é o "cartão de visita" para os pares se conectarem entre si.
        return {"id_jogador": self.id, "nome": self.nome, "host": self.host, "porta": self.porta}


class Partida:
    """Uma sala de espera no lobby (antes de o jogo começar)."""

    def __init__(self, id_partida, nome, max_jogadores, criador):
        self.id = id_partida
        self.nome = nome
        self.max_jogadores = max_jogadores  # 3 ou 4
        self.criador_id = criador.id        # só o criador pode mandar INICIAR_PARTIDA
        self.jogadores = [criador]  # ordem de entrada (o 1º da lista vira criador se o criador sair)
        self.estado = AGUARDANDO

    def resumo(self):
        # Versão curta, usada na LISTA_PARTIDAS (não precisa mostrar quem está dentro).
        return {
            "id_partida": self.id,
            "nome": self.nome,
            "criador": self.criador_id,
            "num_jogadores": len(self.jogadores),
            "max_jogadores": self.max_jogadores,
            "estado": self.estado,
        }

    def detalhes(self):
        # Versão completa: o resumo + a lista de quem está na sala.
        # Usada para quem está dentro da sala (PARTIDA_CRIADA, ENTROU_PARTIDA, ATUALIZACAO_PARTIDA).
        d = self.resumo()
        d["jogadores"] = [{"id_jogador": j.id, "nome": j.nome} for j in self.jogadores]
        return d


class ServidorLobby:
    def __init__(self, host, porta):
        self.endereco = (host, porta)
        # Várias threads (uma por cliente) mexem nos mesmos dicionários ao mesmo tempo.
        # O lock garante que só uma por vez altere jogadores/partidas.
        self._lock = threading.Lock()   # protege jogadores e partidas
        self._jogadores = {}            # id -> Jogador
        self._partidas = {}             # id -> Partida (apenas AGUARDANDO)
        self._ids_jogador = itertools.count(1)  # next() devolve 1, depois 2, 3...
        self._ids_partida = itertools.count(1)
        self._sock = None               # socket de escuta (criado em iniciar())
        self._rodando = threading.Event()  # "bandeira" ligada enquanto o servidor roda

    # ------------------------------------------------------------------ rede
    def iniciar(self):
        # AF_INET = IPv4, SOCK_STREAM = TCP
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Permite reabrir o servidor na mesma porta logo após fechar
        # (sem isso o SO às vezes diz "porta em uso" por alguns segundos).
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(self.endereco)   # "prende" o socket no IP/porta escolhidos
        self._sock.listen()              # passa a aceitar pedidos de conexão
        self.endereco = self._sock.getsockname()  # endereço real (útil se a porta for 0 = aleatória)
        self._rodando.set()
        log.info("Lobby escutando em %s:%d", *self.endereco)

    def servir_para_sempre(self):
        while self._rodando.is_set():
            try:
                # accept() espera alguém conectar e devolve um socket NOVO só para
                # aquele cliente (sock) e o endereço dele (end). O _sock original
                # continua livre para aceitar os próximos.
                sock, end = self._sock.accept()
            except OSError:
                break  # o socket de escuta foi fechado (parar()): sai do laço
            # Uma thread por cliente: assim um cliente parado esperando não trava os outros.
            # daemon=True: a thread morre junto quando o programa principal termina.
            threading.Thread(target=self._atender, args=(sock,), daemon=True,
                             name=f"cliente-{end[0]}:{end[1]}").start()

    def parar(self):
        self._rodando.clear()
        if self._sock:
            self._sock.close()  # faz o accept() parado acima dar OSError e sair

    def _atender(self, sock):
        """Laço de uma conexão de cliente (uma thread por cliente)."""
        # Se o recv() ficar 60 s sem receber nada, lança socket.timeout (tratado lá embaixo).
        sock.settimeout(TIMEOUT_CLIENTE)
        conexao = p.Conexao(sock)   # embrulha o socket para receber mensagens inteiras
        sessao = {"jogador": None}  # estado é derivado em _estado()
        erros = 0                   # quantos erros de protocolo este cliente já cometeu
        log.info("Conexão de %s:%d", *conexao.endereco)
        try:
            while True:
                # 1º: tenta LER uma mensagem.
                try:
                    msg = conexao.receber()
                except p.ErroProtocolo as e:
                    # Chegou lixo (JSON inválido, sem "tipo"...): avisa o cliente e segue.
                    erros += 1
                    conexao.enviar_erro(e.codigo, e.descricao)
                    if erros >= MAX_ERROS_POR_CONEXAO:
                        log.warning("%s: erros demais, desconectando", conexao.endereco)
                        break
                    continue  # volta para o início do while e espera a próxima mensagem
                # 2º: a mensagem é um JSON válido; agora tenta EXECUTAR o pedido.
                try:
                    self._tratar(conexao, sessao, msg)
                except p.ErroProtocolo as e:
                    # Pedido inválido (campo errado, estado errado, sala cheia...).
                    # ref_tipo diz ao cliente QUAL pedido deu erro.
                    erros += 1
                    conexao.enviar_erro(e.codigo, e.descricao, ref_tipo=msg.get("tipo"))
                    if erros >= MAX_ERROS_POR_CONEXAO:
                        break
        except socket.timeout:
            # Cliente ficou 60 s calado (nem PING): consideramos que caiu.
            log.info("%s: inativo por %.0fs, desconectando", conexao.endereco, TIMEOUT_CLIENTE)
        except p.ConexaoFechada:
            pass  # cliente fechou o programa normalmente: nada a avisar
        finally:
            # finally roda SEMPRE (saída normal, erro ou timeout):
            # tira o jogador da sala/lobby e libera o socket.
            self._remover_jogador(sessao["jogador"])
            conexao.fechar()
            log.info("Conexão encerrada: %s:%d", *conexao.endereco)

    # -------------------------------------------------------------- mensagens
    def _tratar(self, conexao, sessao, msg):
        """Decide o que fazer com cada mensagem recebida."""
        tipo = msg["tipo"]
        if tipo == p.PING:
            conexao.enviar(p.PONG)  # PING vale em qualquer estado: só responde "estou vivo"
            return

        # Tabela: tipo da mensagem -> (função que trata, estados em que ela é permitida).
        # Isso É a máquina de estados do lobby escrita como dados.
        tratadores = {
            p.REGISTRAR: (self._registrar, {SEM_REGISTRO}),
            p.LISTAR_PARTIDAS: (self._listar, {REGISTRADO, NA_SALA}),
            p.CRIAR_PARTIDA: (self._criar, {REGISTRADO}),
            p.ENTRAR_PARTIDA: (self._entrar, {REGISTRADO}),
            p.SAIR_PARTIDA: (self._sair, {NA_SALA}),
            p.INICIAR_PARTIDA: (self._iniciar_partida, {NA_SALA}),
        }
        if tipo not in tratadores:
            # Mensagem que não existe no protocolo do lobby (ex.: "JOGAR_CARTA").
            raise p.ErroProtocolo(p.E_TIPO_DESCONHECIDO, f"tipo '{tipo}' não existe no lobby")
        funcao, estados_validos = tratadores[tipo]
        estado = self._estado(sessao)
        if estado not in estados_validos:
            # Mensagem existe, mas fora de hora (ex.: CRIAR_PARTIDA sem ter registrado).
            raise p.ErroProtocolo(p.E_ESTADO_INVALIDO,
                                  f"'{tipo}' não é permitido no estado {estado}")
        funcao(conexao, sessao, msg)  # chama a função certa (_registrar, _criar...)

    def _estado(self, sessao):
        """O estado é derivado dos dados, então nunca fica dessincronizado."""
        # Em vez de guardar o estado numa variável (que poderia ficar desatualizada),
        # ele é CALCULADO: tem jogador? está numa partida?
        jogador = sessao["jogador"]
        if jogador is None:
            return SEM_REGISTRO
        with self._lock:
            return REGISTRADO if jogador.id_partida is None else NA_SALA

    def _registrar(self, conexao, sessao, msg):
        # Valida os campos: se algo estiver errado, campo_str/campo_int lançam ErroProtocolo.
        nome = p.campo_str(msg, "nome", 1, 20)
        porta = p.campo_int(msg, "porta_p2p", 1, 65535)  # porta onde o jogador escuta os pares
        # Por padrão o host P2P é o IP de onde a conexão veio (funciona na LAN).
        # O cliente pode informar outro (ex.: máquina com várias interfaces).
        host = msg.get("host_p2p") or conexao.endereco[0]
        if not isinstance(host, str) or len(host) > 255:
            raise p.ErroProtocolo(p.E_CAMPO_INVALIDO, "'host_p2p' inválido")
        with self._lock:
            # Nomes únicos (sem diferenciar maiúscula/minúscula) para não confundir na partida.
            if any(j.nome.lower() == nome.lower() for j in self._jogadores.values()):
                raise p.ErroProtocolo(p.E_NOME_EM_USO, f"já existe um jogador '{nome}'")
            jogador = Jogador(next(self._ids_jogador), nome, host, porta, conexao)
            self._jogadores[jogador.id] = jogador
        sessao["jogador"] = jogador  # a partir daqui o estado vira REGISTRADO
        conexao.enviar(p.REGISTRADO, id_jogador=jogador.id)  # devolve o id ao jogador
        log.info("Registrado: %s (id=%d, p2p=%s:%d)", nome, jogador.id, host, porta)

    def _listar(self, conexao, sessao, msg):
        with self._lock:
            partidas = [pt.resumo() for pt in self._partidas.values()]
        conexao.enviar(p.LISTA_PARTIDAS, partidas=partidas)  # envia depois de soltar o lock

    def _criar(self, conexao, sessao, msg):
        nome = p.campo_str(msg, "nome_partida", 1, 30)
        # max_jogadores é opcional: se não vier, usa 4.
        maximo = msg.get("max_jogadores", p.MAX_JOGADORES)
        # Truque para reaproveitar campo_int (que lê de um dict): garante 3 <= maximo <= 4.
        maximo = p.campo_int({"m": maximo}, "m", p.MIN_JOGADORES, p.MAX_JOGADORES)
        jogador = sessao["jogador"]
        with self._lock:
            partida = Partida(next(self._ids_partida), nome, maximo, jogador)
            self._partidas[partida.id] = partida
            jogador.id_partida = partida.id  # criador já entra na sala -> estado NA_SALA
            detalhes = partida.detalhes()    # copia os dados ainda dentro do lock
        conexao.enviar(p.PARTIDA_CRIADA, partida=detalhes)
        log.info("Partida %d '%s' criada por %s", partida.id, nome, jogador.nome)

    def _entrar(self, conexao, sessao, msg):
        id_partida = p.campo_int(msg, "id_partida", 1)
        jogador = sessao["jogador"]
        with self._lock:
            partida = self._partidas.get(id_partida)  # .get devolve None se não existir
            if partida is None:
                raise p.ErroProtocolo(p.E_PARTIDA_INEXISTENTE, f"partida {id_partida} não existe")
            if len(partida.jogadores) >= partida.max_jogadores:
                raise p.ErroProtocolo(p.E_PARTIDA_CHEIA, f"partida {id_partida} está cheia")
            partida.jogadores.append(jogador)
            jogador.id_partida = partida.id
            detalhes = partida.detalhes()
            outros = [j for j in partida.jogadores if j is not jogador]  # quem já estava na sala
        conexao.enviar(p.ENTROU_PARTIDA, partida=detalhes)  # resposta para quem entrou
        # Avisa quem já estava na sala que chegou alguém novo.
        self._notificar(outros, p.ATUALIZACAO_PARTIDA, partida=detalhes)
        log.info("%s entrou na partida %d", jogador.nome, id_partida)

    def _sair(self, conexao, sessao, msg):
        jogador = sessao["jogador"]
        self._tirar_da_partida(jogador)  # também avisa os que ficaram
        conexao.enviar(p.SAIU_PARTIDA)

    def _iniciar_partida(self, conexao, sessao, msg):
        jogador = sessao["jogador"]
        with self._lock:
            partida = self._partidas.get(jogador.id_partida)
            if partida is None:
                raise p.ErroProtocolo(p.E_PARTIDA_INEXISTENTE, "você não está em uma partida")
            if partida.criador_id != jogador.id:
                raise p.ErroProtocolo(p.E_NAO_AUTORIZADO, "apenas o criador pode iniciar")
            if len(partida.jogadores) < p.MIN_JOGADORES:
                raise p.ErroProtocolo(p.E_JOGADORES_INSUFICIENTES,
                                      f"são necessários pelo menos {p.MIN_JOGADORES} jogadores")
            # A partida sai do lobby: daqui em diante é responsabilidade dos pares.
            del self._partidas[partida.id]
            membros = list(partida.jogadores)  # cópia da lista para usar fora do lock
            for j in membros:
                j.id_partida = None
        # Como id_partida foi zerado, todos os membros voltam ao estado REGISTRADO
        # no lobby (podem, inclusive, entrar em outra partida depois).
        conexao.enviar(p.INICIO_ACEITO, id_partida=partida.id)  # resposta só para o criador
        # Notificação para TODOS os membros (inclusive o criador): é com ela que cada
        # jogador descobre onde estão os outros e passa a falar direto com eles (P2P).
        self._notificar(
            membros, p.PARTIDA_INICIADA,
            id_partida=partida.id,
            # Segredo compartilhado só pelos membros: usado no handshake P2P
            # para que um processo de fora não entre na partida.
            chave_partida=secrets.token_hex(16),  # 16 bytes aleatórios em hexadecimal (32 caracteres)
            # Ordem crescente de id_jogador: todos os pares recebem a MESMA lista,
            # o que serve de ordem inicial de turnos determinística.
            jogadores=sorted((j.como_par() for j in membros), key=lambda d: d["id_jogador"]),
        )
        log.info("Partida %d iniciada com %s", partida.id, [j.nome for j in membros])

    # ------------------------------------------------------------- auxiliares
    def _notificar(self, destinatarios, tipo, **campos):
        """Envia fora do lock: um cliente lento não trava o lobby inteiro."""
        # Se o envio para um jogador travasse DENTRO do lock, todas as outras
        # threads (outros clientes) ficariam esperando. Por isso chamamos fora dele.
        # (O parâmetro NÃO pode se chamar "jogadores": esse nome já é um campo da
        # mensagem PARTIDA_INICIADA, e os dois colidiriam dentro de **campos.)
        for j in destinatarios:
            try:
                j.conexao.enviar(tipo, **campos)
            except p.ConexaoFechada:
                pass  # esse jogador caiu; a thread dele mesma vai fazer a limpeza

    def _tirar_da_partida(self, jogador):
        """Tira o jogador da sala em que estiver e avisa os que ficaram."""
        with self._lock:
            partida = self._partidas.get(jogador.id_partida)
            jogador.id_partida = None
            if partida is None:
                return  # não estava em sala nenhuma (ou a partida já começou)
            partida.jogadores.remove(jogador)
            if not partida.jogadores:
                # Sala ficou vazia: apaga.
                del self._partidas[partida.id]
                log.info("Partida %d removida (vazia)", partida.id)
                return
            if partida.criador_id == jogador.id:
                # O mais antigo na sala herda o papel de criador.
                partida.criador_id = partida.jogadores[0].id
            restantes = list(partida.jogadores)
            detalhes = partida.detalhes()
        self._notificar(restantes, p.ATUALIZACAO_PARTIDA, partida=detalhes)
        log.info("%s saiu da partida %d", jogador.nome, partida.id)

    def _remover_jogador(self, jogador):
        """Chamado quando a conexão do jogador acaba: some do lobby por completo."""
        if jogador is None:
            return  # conectou e caiu sem nem se registrar
        self._tirar_da_partida(jogador)
        with self._lock:
            self._jogadores.pop(jogador.id, None)  # pop com None não dá erro se já saiu


def main():
    # Endereço e porta vêm da linha de comando, não fixos no código (requisito do trabalho).
    parser = argparse.ArgumentParser(description="Servidor de lobby do jogo de cartas P2P")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface de escuta (padrão: 0.0.0.0 = todas)")
    parser.add_argument("--porta", type=int, default=5000, help="porta TCP (padrão: 5000)")
    parser.add_argument("-v", "--verbose", action="store_true")  # -v mostra logs de depuração
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    servidor = ServidorLobby(args.host, args.porta)
    servidor.iniciar()
    try:
        servidor.servir_para_sempre()  # fica aqui até Ctrl+C
    except KeyboardInterrupt:
        log.info("Encerrando lobby")
    finally:
        servidor.parar()


# Só roda main() se este arquivo for executado direto (python servidor_lobby.py),
# e não quando for importado por outro arquivo (ex.: nos testes).
if __name__ == "__main__":
    main()
