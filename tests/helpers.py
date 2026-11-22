"""Infraestrutura de testes: estado comparável e execução com prazo limitado."""
import copy
import queue
import threading
import time
from unittest.mock import Mock

from jogo_p2p import JogoP2P, AGUARDANDO_ACAO


def jogadores(n=3):
    return [dict(id_jogador=i, nome=f'Jogador {i}', host='127.0.0.1', porta=6000+i)
            for i in range(1, n+1)]


def jogo_pronto():
    rede = Mock()
    rede.proximo_evento.return_value = None
    jogo = JogoP2P(1, jogadores(), rede)
    jogo.estado = AGUARDANDO_ACAO
    jogo.maos = {1: [('Azul', '1'), ('Verde', '2')],
                 2: [('Azul', '3'), ('Amarelo', '4')],
                 3: [('Azul', '5'), ('Vermelho', '6')]}
    jogo.pilha_descarte = [('Azul', '0')]
    jogo.baralho = [('Verde', '8'), ('Amarelo', '9'), ('Azul', '7')]
    return jogo


def estado(jogo):
    """Só estado replicado: exclui identidade local, rede e buffers de transporte."""
    return copy.deepcopy((jogo.ativos, jogo.maos, jogo.baralho, jogo.pilha_descarte,
                          jogo.sentido, jogo.idx_atual, jogo.estado, jogo.seq,
                          jogo.carta_comprada_pendente, jogo.vencedor))


def paralelo(funcoes, timeout=15):
    """Propaga exceções das threads e falha com prazo, sem join ilimitado."""
    resultados = queue.Queue()

    def executar(i, fn):
        try:
            resultados.put((i, fn(), None))
        except BaseException as erro:
            resultados.put((i, None, erro))

    threads = [threading.Thread(target=executar, args=(i, fn), daemon=True)
               for i, fn in enumerate(funcoes)]
    for thread in threads:
        thread.start()
    prazo = time.monotonic() + timeout
    saida = [None] * len(threads)
    for _ in threads:
        try:
            i, valor, erro = resultados.get(timeout=max(0.001, prazo-time.monotonic()))
        except queue.Empty:
            raise AssertionError('Operação concorrente excedeu o prazo de teste')
        if erro is not None:
            raise erro
        saida[i] = valor
    for thread in threads:
        thread.join(timeout=max(0.001, prazo-time.monotonic()))
    return saida
