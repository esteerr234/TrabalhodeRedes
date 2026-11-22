"""
Motor de estado distribuído do jogo de cartas P2P (Opção 2 — Uno sem servidor).

Responsabilidades:
  * manter a réplica completa e determinística do estado da partida em cada par;
  * executar o protocolo Commit-Reveal (SHA-256) para embaralhamento justo tanto
    no início da partida quanto na reciclagem do monte de descarte;
  * validar independentemente todas as ações recebidas dos demais pares (rejeitando
    jogadas fora de turno ou contrárias às regras com ERRO, sem alterar o estado);
  * tratar desconexões de pares: remover o jogador, descartar suas cartas de forma
    determinística, avançar o turno se fosse a vez dele, desbloquear Commit-Reveal
    em andamento e encerrar a partida se restar apenas 1 jogador ativo.
"""

import hashlib
import logging
import secrets

import baralho
import protocolo as p
import rede_p2p
import regras

log = logging.getLogger("jogo_p2p")

# Estados da máquina de estados do jogo P2P
EMBARALHANDO_INICIAL = "EMBARALHANDO_INICIAL"
AGUARDANDO_ACAO = "AGUARDANDO_ACAO"
AGUARDANDO_POS_COMPRA = "AGUARDANDO_POS_COMPRA"
RECICLANDO_BARALHO = "RECICLANDO_BARALHO"
ENCERRADA = "ENCERRADA"


class JogoP2P:
    """Réplica determinística do estado da partida em um par."""

    def __init__(self, meu_id, jogadores, rede, on_evento=None):
        """
        jogadores: lista ordenada de dicts {"id_jogador", "nome", "host", "porta"}.
        rede: instância de rede_p2p.RedeP2P já conectada à partida.
        on_evento: callback opcional fn(texto) para exibir acontecimentos na UI.
        """
        self.meu_id = meu_id
        self.rede = rede
        self._on_evento = on_evento or (lambda msg: None)

        jogadores_ord = sorted(jogadores, key=lambda j: j["id_jogador"])
        self.nomes = {j["id_jogador"]: j["nome"] for j in jogadores_ord}
        self.ativos = [j["id_jogador"] for j in jogadores_ord]
        self.maos = {pid: [] for pid in self.ativos}

        self.baralho = []
        self.pilha_descarte = []
        self.sentido = 1
        self.idx_atual = 0

        self.estado = EMBARALHANDO_INICIAL
        self.carta_comprada_pendente = None
        self.vencedor = None
        self.motivo_encerramento = ""

        # Estado do protocolo Commit-Reveal indexado por número de rodada
        self.rodada_embaralhamento = 0
        self._commits = {}           # rodada -> {pid: hash_sha256}
        self._revelacoes = {}        # rodada -> {pid: semente_validada}
        self._revelacoes_brutas = {} # rodada -> {pid: semente_recebida_antes_do_commit}

        # Ordenação causal de ações entre múltiplos sockets TCP (full mesh)
        self.seq = 0
        self._acoes_futuras = {}     # seq_esperado -> (remetente, msg)
        self._acoes_pendentes = []   # ações recebidas durante Commit-Reveal

    # ------------------------------------------------------------ propriedades
    @property
    def jogador_atual_id(self):
        if not self.ativos:
            return None
        return self.ativos[self.idx_atual]

    @property
    def topo_descarte(self):
        return self.pilha_descarte[-1] if self.pilha_descarte else None

    @property
    def minha_mao(self):
        return self.maos.get(self.meu_id, [])

    def eh_minha_vez(self):
        return (
            self.estado in (AGUARDANDO_ACAO, AGUARDANDO_POS_COMPRA)
            and self.jogador_atual_id == self.meu_id
        )

    def nome_jogador(self, pid):
        return self.nomes.get(pid, f"Jogador {pid}")

    def _notificar(self, texto):
        log.info("[J%d] %s", self.meu_id, texto)
        self._on_evento(texto)

    # -------------------------------------------------------- ciclo de início
    def iniciar(self):
        """
        Executa o embaralhamento justo inicial (Commit-Reveal rodada 1),
        distribui 7 cartas para cada jogador ativo e vira a carta inicial.
        """
        self._notificar("Iniciando embaralhamento justo distribuído (Commit-Reveal)...")
        semente_final = self._executar_commit_reveal()
        if semente_final is None:
            return False

        self.baralho = baralho.criar_baralho_canonico()
        baralho.embaralhar_deterministico(self.baralho, semente_final)

        for pid in self.ativos:
            self.maos[pid] = [self.baralho.pop() for _ in range(7)]

        while True:
            carta_inicial = self.baralho.pop()
            if carta_inicial[1] not in ("Pular", "Inverter", "+2"):
                self.pilha_descarte.append(carta_inicial)
                break
            self.baralho.insert(0, carta_inicial)

        self.idx_atual = 0
        self.sentido = 1
        self.estado = AGUARDANDO_ACAO
        self._notificar(
            f"Partida iniciada! Carta na mesa: {self.topo_descarte}. "
            f"Vez de {self.nome_jogador(self.jogador_atual_id)}."
        )
        self._drenar_acoes_pendentes()
        return True

    # ----------------------------------------------------------- commit-reveal
    def _executar_commit_reveal(self):
        """
        Protocolo Commit-Reveal distribuído com SHA-256:
          1. Cada par ativo gera semente aleatória e envia COMMIT_SEMENTE(SHA-256).
          2. Após receber o commit de TODOS os pares ativos, envia REVELAR_SEMENTE.
          3. Valida SHA-256(semente) == commit para cada par.
          4. Combina sementes ordenadas por id_jogador e gera inteiro via SHA-256.
        """
        self.rodada_embaralhamento += 1
        rodada = self.rodada_embaralhamento
        self.estado = EMBARALHANDO_INICIAL if rodada == 1 else RECICLANDO_BARALHO

        self._commits.setdefault(rodada, {})
        self._revelacoes.setdefault(rodada, {})
        self._revelacoes_brutas.setdefault(rodada, {})

        minha_semente = secrets.token_hex(16)
        meu_commit = hashlib.sha256(minha_semente.encode("utf-8")).hexdigest()
        self._commits[rodada][self.meu_id] = meu_commit

        self.rede.broadcast(
            p.COMMIT_SEMENTE,
            rodada=rodada,
            id_jogador=self.meu_id,
            commit=meu_commit,
        )

        # Fase 1: aguarda COMMIT_SEMENTE de todos os pares ativos
        while self.estado != ENCERRADA and not all(
            pid in self._commits[rodada] for pid in self.ativos
        ):
            evento = self.rede.proximo_evento(timeout=0.2)
            if evento is not None:
                self._processar_evento_durante_embaralhamento(evento)

        if self.estado == ENCERRADA:
            return None

        # Fase 2: todos comprometeram suas sementes -> revela a própria semente
        self._revelacoes[rodada][self.meu_id] = minha_semente
        self.rede.broadcast(
            p.REVELAR_SEMENTE,
            rodada=rodada,
            id_jogador=self.meu_id,
            semente=minha_semente,
        )

        # Valida eventuais revelações que tenham chegado antes do último commit
        for pid, semente_bruta in list(self._revelacoes_brutas[rodada].items()):
            self._validar_e_registrar_revelacao(rodada, pid, semente_bruta)
        self._revelacoes_brutas[rodada].clear()

        while self.estado != ENCERRADA and not all(
            pid in self._revelacoes[rodada] for pid in self.ativos
        ):
            evento = self.rede.proximo_evento(timeout=0.2)
            if evento is not None:
                self._processar_evento_durante_embaralhamento(evento)

        if self.estado == ENCERRADA:
            return None

        ids_ordenados = sorted(self.ativos)
        combinado = "|".join(
            f"{pid}:{self._revelacoes[rodada][pid]}" for pid in ids_ordenados
        )
        semente_final = int(hashlib.sha256(combinado.encode("utf-8")).hexdigest(), 16)
        log.info(
            "[J%d] Commit-Reveal rodada %d concluído com pares %s",
            self.meu_id,
            rodada,
            ids_ordenados,
        )
        return semente_final

    def _processar_evento_durante_embaralhamento(self, evento):
        if evento["evento"] == rede_p2p.EVENTO_PAR_DESCONECTADO:
            self._tratar_desconexao(evento["id_jogador"], evento["motivo"])
            return

        remetente = evento["remetente"]
        msg = evento["msg"]
        tipo = msg.get("tipo")
        if tipo in (p.COMMIT_SEMENTE, p.REVELAR_SEMENTE):
            self._registrar_msg_embaralhamento(remetente, msg)
        elif tipo == p.ERRO:
            log.warning("[J%d] ERRO recebido do par %d: %s", self.meu_id, remetente, msg)
        else:
            # Ação de jogo recebida adiantada: guarda para processar após o embaralhamento
            self._acoes_pendentes.append((remetente, msg))

    def _registrar_msg_embaralhamento(self, remetente, msg):
        tipo = msg.get("tipo")
        try:
            rodada = p.campo_int(msg, "rodada", 1)
        except p.ErroProtocolo as e:
            self.rede.enviar_para(
                remetente, p.ERRO, codigo=e.codigo, descricao=e.descricao, ref_tipo=tipo
            )
            return

        self._commits.setdefault(rodada, {})
        self._revelacoes.setdefault(rodada, {})
        self._revelacoes_brutas.setdefault(rodada, {})

        if tipo == p.COMMIT_SEMENTE:
            commit = msg.get("commit")
            if not isinstance(commit, str) or len(commit) != 64:
                self.rede.enviar_para(
                    remetente,
                    p.ERRO,
                    codigo=p.E_CAMPO_INVALIDO,
                    descricao="commit SHA-256 inválido",
                    ref_tipo=tipo,
                )
                return
            if remetente not in self._commits[rodada]:
                self._commits[rodada][remetente] = commit
                # Se já havia uma revelação adiantada desse par, valida agora
                if remetente in self._revelacoes_brutas[rodada]:
                    semente = self._revelacoes_brutas[rodada].pop(remetente)
                    self._validar_e_registrar_revelacao(rodada, remetente, semente)

        elif tipo == p.REVELAR_SEMENTE:
            semente = msg.get("semente")
            if not isinstance(semente, str) or not semente:
                self.rede.enviar_para(
                    remetente,
                    p.ERRO,
                    codigo=p.E_CAMPO_INVALIDO,
                    descricao="semente ausente ou inválida",
                    ref_tipo=tipo,
                )
                return
            if remetente not in self._commits[rodada]:
                self._revelacoes_brutas[rodada][remetente] = semente
            else:
                self._validar_e_registrar_revelacao(rodada, remetente, semente)

    def _validar_e_registrar_revelacao(self, rodada, remetente, semente):
        commit_esperado = self._commits[rodada].get(remetente)
        if commit_esperado is None:
            return
        hash_calculado = hashlib.sha256(semente.encode("utf-8")).hexdigest()
        if hash_calculado != commit_esperado:
            log.warning(
                "[J%d] Semente inválida do par %d na rodada %d (hash não confere)",
                self.meu_id,
                remetente,
                rodada,
            )
            self.rede.enviar_para(
                remetente,
                p.ERRO,
                codigo=p.E_SEMENTE_INVALIDA,
                descricao="SHA-256(semente) não confere com o commit enviado",
                ref_tipo=p.REVELAR_SEMENTE,
            )
            return
        self._revelacoes[rodada][remetente] = semente

    # ------------------------------------------------------ ações do jogador
    def jogar_carta(self, carta):
        """Ação local: joga uma carta da mão na nossa vez."""
        if not self.eh_minha_vez() or self.estado != AGUARDANDO_ACAO:
            raise ValueError("Não é possível jogar agora.")
        carta = (carta[0], carta[1])
        if carta not in self.minha_mao:
            raise ValueError("Carta não está na sua mão.")
        if not regras.pode_jogar(carta, self.topo_descarte):
            raise ValueError("Essa carta não pode ser jogada sobre o topo do descarte.")

        self.rede.broadcast(p.ACAO_JOGAR, seq=self.seq + 1, carta=list(carta))
        self._aplicar_jogada(self.meu_id, carta)

    def comprar_carta(self):
        """
        Ação local: compra 1 carta quando não há cartas válidas na mão (Regra 4).
        Retorna a carta comprada (ou None se não houver cartas disponíveis).
        """
        if not self.eh_minha_vez() or self.estado != AGUARDANDO_ACAO:
            raise ValueError("Não é possível comprar agora.")
        if regras.cartas_validas(self.minha_mao, self.topo_descarte):
            raise ValueError("Você possui cartas válidas e não pode comprar.")

        self.rede.broadcast(p.ACAO_COMPRAR, seq=self.seq + 1)
        return self._aplicar_compra(self.meu_id)

    def jogar_carta_comprada(self):
        """Ação local: joga imediatamente a carta recém-comprada (Regra 4)."""
        if not self.eh_minha_vez() or self.estado != AGUARDANDO_POS_COMPRA:
            raise ValueError("Não há carta comprada aguardando decisão.")
        carta = self.carta_comprada_pendente
        self.rede.broadcast(p.ACAO_JOGAR_COMPRADA, seq=self.seq + 1, carta=list(carta))
        self._aplicar_jogar_comprada(self.meu_id, carta)

    def passar_vez(self):
        """Ação local: recusa jogar a carta recém-comprada e passa a vez."""
        if not self.eh_minha_vez() or self.estado != AGUARDANDO_POS_COMPRA:
            raise ValueError("Não é possível passar a vez neste estado.")
        self.rede.broadcast(p.ACAO_PASSAR, seq=self.seq + 1)
        self._aplicar_passar(self.meu_id)

    # ---------------------------------------------- processamento de eventos
    def processar_eventos_pendentes(self):
        """Processa todas as mensagens/desconexões já presentes na fila (não bloqueia)."""
        self._drenar_acoes_pendentes()
        while self.estado != ENCERRADA:
            evento = self.rede.proximo_evento(timeout=0)
            if evento is None:
                break
            self._processar_evento(evento)

    def aguardar_evento(self, timeout=0.5):
        """Aguarda e processa um evento de rede (ou retorna após timeout)."""
        self._drenar_acoes_pendentes()
        if self.estado == ENCERRADA:
            return
        evento = self.rede.proximo_evento(timeout=timeout)
        if evento is not None:
            self._processar_evento(evento)

    def _drenar_acoes_pendentes(self):
        while self._acoes_pendentes and self.estado != ENCERRADA:
            remetente, msg = self._acoes_pendentes.pop(0)
            self._tratar_mensagem(remetente, msg)
        self._drenar_acoes_futuras()

    def _drenar_acoes_futuras(self):
        while self.estado != ENCERRADA and (self.seq + 1) in self._acoes_futuras:
            remetente, msg = self._acoes_futuras.pop(self.seq + 1)
            self._tratar_mensagem(remetente, msg)

    def _processar_evento(self, evento):
        if evento["evento"] == rede_p2p.EVENTO_PAR_DESCONECTADO:
            self._tratar_desconexao(evento["id_jogador"], evento["motivo"])
        elif evento["evento"] == rede_p2p.EVENTO_MENSAGEM:
            self._tratar_mensagem(evento["remetente"], evento["msg"])

    def _tratar_mensagem(self, remetente, msg):
        if self.estado == ENCERRADA:
            return
        tipo = msg.get("tipo")

        if tipo in (p.COMMIT_SEMENTE, p.REVELAR_SEMENTE):
            self._registrar_msg_embaralhamento(remetente, msg)
            return
        if tipo == p.ERRO:
            log.warning("[J%d] ERRO recebido do par %d: %s", self.meu_id, remetente, msg)
            return

        if tipo not in p.TIPOS_JOGO_P2P:
            self.rede.enviar_para(
                remetente,
                p.ERRO,
                codigo=p.E_TIPO_DESCONHECIDO,
                descricao=f"tipo '{tipo}' desconhecido na partida",
                ref_tipo=tipo,
            )
            return

        # Ordenação causal: se a ação tem seq futuro (chegou antes da ação
        # anterior por outro socket TCP), guarda no buffer até self.seq avançar.
        try:
            seq_msg = p.campo_int(msg, "seq")
        except p.ErroProtocolo as e:
            self._rejeitar(remetente, e.codigo, e.descricao, tipo)
            return
        if seq_msg > self.seq + 1:
            if seq_msg <= self.seq + len(self.ativos) + 4 and seq_msg not in self._acoes_futuras:
                self._acoes_futuras[seq_msg] = (remetente, msg)
                return
            self._rejeitar(remetente, p.E_FORA_DE_TURNO, "número de sequência fora da janela", tipo)
            return
        if seq_msg <= self.seq:
            self._rejeitar(remetente, p.E_FORA_DE_TURNO, "ação com sequência obsoleta", tipo)
            return

        # Validação independente de turno
        if remetente != self.jogador_atual_id:
            log.warning(
                "[J%d] Jogada fora de turno rejeitada de %s (%d); vez de %s (%d)",
                self.meu_id,
                self.nome_jogador(remetente),
                remetente,
                self.nome_jogador(self.jogador_atual_id),
                self.jogador_atual_id,
            )
            self.rede.enviar_para(
                remetente,
                p.ERRO,
                codigo=p.E_FORA_DE_TURNO,
                descricao="não é a sua vez",
                ref_tipo=tipo,
            )
            return

        # Validação de estado e regras para cada tipo de ação
        if tipo == p.ACAO_JOGAR:
            if self.estado != AGUARDANDO_ACAO:
                self._rejeitar(remetente, p.E_ESTADO_INVALIDO, "estado não permite ACAO_JOGAR", tipo)
                return
            try:
                carta = p.campo_carta(msg, "carta")
            except p.ErroProtocolo as e:
                self._rejeitar(remetente, e.codigo, e.descricao, tipo)
                return
            if carta not in self.maos[remetente]:
                self._rejeitar(remetente, p.E_JOGADA_INVALIDA, "carta não pertence à mão do jogador", tipo)
                return
            if not regras.pode_jogar(carta, self.topo_descarte):
                self._rejeitar(remetente, p.E_JOGADA_INVALIDA, "carta incompatível com o topo do descarte", tipo)
                return
            self._aplicar_jogada(remetente, carta)

        elif tipo == p.ACAO_COMPRAR:
            if self.estado != AGUARDANDO_ACAO:
                self._rejeitar(remetente, p.E_ESTADO_INVALIDO, "estado não permite ACAO_COMPRAR", tipo)
                return
            if regras.cartas_validas(self.maos[remetente], self.topo_descarte):
                self._rejeitar(
                    remetente,
                    p.E_JOGADA_INVALIDA,
                    "jogador possui cartas válidas e não pode comprar",
                    tipo,
                )
                return
            self._aplicar_compra(remetente)

        elif tipo == p.ACAO_JOGAR_COMPRADA:
            if self.estado != AGUARDANDO_POS_COMPRA:
                self._rejeitar(remetente, p.E_ESTADO_INVALIDO, "estado não permite ACAO_JOGAR_COMPRADA", tipo)
                return
            try:
                carta = p.campo_carta(msg, "carta")
            except p.ErroProtocolo as e:
                self._rejeitar(remetente, e.codigo, e.descricao, tipo)
                return
            if carta != self.carta_comprada_pendente or carta not in self.maos[remetente]:
                self._rejeitar(remetente, p.E_JOGADA_INVALIDA, "carta não corresponde à carta comprada", tipo)
                return
            if not regras.pode_jogar(carta, self.topo_descarte):
                self._rejeitar(remetente, p.E_JOGADA_INVALIDA, "carta comprada não é jogável", tipo)
                return
            self._aplicar_jogar_comprada(remetente, carta)

        elif tipo == p.ACAO_PASSAR:
            if self.estado != AGUARDANDO_POS_COMPRA:
                self._rejeitar(remetente, p.E_ESTADO_INVALIDO, "estado não permite ACAO_PASSAR", tipo)
                return
            self._aplicar_passar(remetente)

    def _rejeitar(self, remetente, codigo, descricao, ref_tipo):
        log.warning(
            "[J%d] Ação inválida rejeitada de %s (%d): %s - %s",
            self.meu_id,
            self.nome_jogador(remetente),
            remetente,
            codigo,
            descricao,
        )
        self.rede.enviar_para(
            remetente, p.ERRO, codigo=codigo, descricao=descricao, ref_tipo=ref_tipo
        )

    # ------------------------------------------ transições determinísticas
    def _comprar_do_monte(self):
        """
        Retira 1 carta do topo do monte de compra. Se o monte estiver vazio e
        houver cartas recicláveis no descarte, executa Commit-Reveal para
        reciclar o baralho de forma justa e idêntica em todos os pares.
        """
        if len(self.baralho) == 0:
            if len(self.pilha_descarte) <= 1:
                self._notificar("Não existem mais cartas para comprar.")
                return None

            self._notificar("Monte de compra acabou! Reciclando descarte via Commit-Reveal...")
            semente_final = self._executar_commit_reveal()
            if semente_final is None:
                return None

            topo = self.pilha_descarte[-1]
            recicladas = sorted(self.pilha_descarte[:-1])
            baralho.embaralhar_deterministico(recicladas, semente_final)
            self.baralho = recicladas
            self.pilha_descarte = [topo]
            self.estado = AGUARDANDO_ACAO
            self._notificar(f"Monte reciclado com {len(self.baralho)} cartas.")

        return self.baralho.pop() if self.baralho else None

    def _aplicar_jogada(self, pid, carta):
        self.seq += 1
        self.maos[pid].remove(carta)
        self.pilha_descarte.append(carta)
        self._notificar(f"{self.nome_jogador(pid)} jogou {carta}.")

        if len(self.maos[pid]) == 0:
            self.vencedor = pid
            self.estado = ENCERRADA
            self.motivo_encerramento = f"{self.nome_jogador(pid)} ficou sem cartas na mão!"
            self._notificar(f"VITORIA! {self.motivo_encerramento}")
            return

        cartas_comprar, pular_proximo, self.sentido = regras.aplicar_efeito(
            carta, self.sentido
        )

        proximo_idx = regras.proximo_jogador(
            self.idx_atual, len(self.ativos), self.sentido
        )
        proximo_id = self.ativos[proximo_idx]

        if cartas_comprar > 0:
            compradas = 0
            for _ in range(cartas_comprar):
                c = self._comprar_do_monte()
                if self.estado == ENCERRADA:
                    return
                if c is not None and proximo_id in self.maos:
                    self.maos[proximo_id].append(c)
                    compradas += 1
            if proximo_id in self.ativos:
                self._notificar(
                    f"{self.nome_jogador(proximo_id)} comprou {compradas} carta(s) e perdeu a vez!"
                )
                proximo_idx = self.ativos.index(proximo_id)
                self.idx_atual = regras.proximo_jogador(
                    proximo_idx, len(self.ativos), self.sentido
                )
            elif pid in self.ativos:
                pid_idx = self.ativos.index(pid)
                self.idx_atual = regras.proximo_jogador(
                    pid_idx, len(self.ativos), self.sentido
                )
        elif pular_proximo:
            self._notificar(f"{self.nome_jogador(proximo_id)} perdeu a vez!")
            self.idx_atual = regras.proximo_jogador(
                proximo_idx, len(self.ativos), self.sentido
            )
        else:
            self.idx_atual = proximo_idx

        self.estado = AGUARDANDO_ACAO
        self._drenar_acoes_futuras()

    def _aplicar_compra(self, pid):
        self.seq += 1
        self._notificar(f"{self.nome_jogador(pid)} não tem cartas válidas e compra 1 carta...")
        nova_carta = self._comprar_do_monte()
        if self.estado == ENCERRADA:
            return None

        if nova_carta is None:
            self._notificar(f"{self.nome_jogador(pid)} não pôde comprar e passou a vez.")
            self.idx_atual = regras.proximo_jogador(
                self.idx_atual, len(self.ativos), self.sentido
            )
            self.estado = AGUARDANDO_ACAO
            self._drenar_acoes_futuras()
            return None

        self.maos[pid].append(nova_carta)
        self._notificar(f"{self.nome_jogador(pid)} comprou {nova_carta}.")

        if regras.pode_jogar(nova_carta, self.topo_descarte):
            self.carta_comprada_pendente = nova_carta
            self.estado = AGUARDANDO_POS_COMPRA
        else:
            self._notificar(
                f"Carta {nova_carta} não pode ser jogada. Vez passa ao próximo jogador."
            )
            self.idx_atual = regras.proximo_jogador(
                self.idx_atual, len(self.ativos), self.sentido
            )
            self.estado = AGUARDANDO_ACAO
        self._drenar_acoes_futuras()
        return nova_carta

    def _aplicar_jogar_comprada(self, pid, carta):
        self.carta_comprada_pendente = None
        self.estado = AGUARDANDO_ACAO
        self._aplicar_jogada(pid, carta)

    def _aplicar_passar(self, pid):
        self.seq += 1
        self.carta_comprada_pendente = None
        self.estado = AGUARDANDO_ACAO
        self._notificar(f"{self.nome_jogador(pid)} optou por não jogar a carta comprada e passou a vez.")
        self.idx_atual = regras.proximo_jogador(
            self.idx_atual, len(self.ativos), self.sentido
        )
        self._drenar_acoes_futuras()

    # ---------------------------------------------------- tolerância a falhas
    def _tratar_desconexao(self, pid_caido, motivo):
        """
        Remove o par desconectado da partida de forma determinística:
          * destino das cartas: removidas do jogo (descartadas fora da pilha,
            preservando a ordem atual do monte de compra e do descarte);
          * se restar apenas 1 jogador ativo, encerra a partida declarando-o vencedor;
          * se era a vez do jogador que caiu, avança o turno deterministicamente
            no sentido atual.
        """
        if pid_caido not in self.ativos:
            return

        idx_caido = self.ativos.index(pid_caido)
        qtd_cartas = len(self.maos.get(pid_caido, []))
        self.ativos.remove(pid_caido)
        self.maos.pop(pid_caido, None)

        self._notificar(
            f"Jogador {self.nome_jogador(pid_caido)} desconectou ({motivo}). "
            f"Suas {qtd_cartas} carta(s) foram removidas do jogo."
        )

        if len(self.ativos) <= 1:
            self.vencedor = self.ativos[0] if self.ativos else None
            self.estado = ENCERRADA
            if self.vencedor is not None:
                self.motivo_encerramento = (
                    f"{self.nome_jogador(self.vencedor)} venceu por ser o único jogador restante!"
                )
            else:
                self.motivo_encerramento = "Partida encerrada sem jogadores ativos."
            self._notificar(f"FIM DE JOGO! {self.motivo_encerramento}")
            return

        # Ajusta o ponteiro de turno (idx_atual) mantendo determinismo em todos os pares
        if idx_caido < self.idx_atual:
            self.idx_atual -= 1
        elif idx_caido == self.idx_atual:
            self.carta_comprada_pendente = None
            if self.estado == AGUARDANDO_POS_COMPRA:
                self.estado = AGUARDANDO_ACAO
            if self.sentido == 1:
                self.idx_atual = idx_caido % len(self.ativos)
            else:
                self.idx_atual = (idx_caido - 1) % len(self.ativos)
            if self.estado == AGUARDANDO_ACAO:
                self._notificar(
                    f"Turno avançado automaticamente para {self.nome_jogador(self.jogador_atual_id)}."
                )
