"""Integração real em loopback, portas efêmeras e lobby em processo isolado."""
import multiprocessing
import socket
import subprocess
import sys
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import baralho
import protocolo as p
import regras
from cliente_lobby import ClienteLobby, ErroLobby, LOBBY_DESCONECTADO
from servidor_lobby import ServidorLobby
from rede_p2p import RedeP2P, EVENTO_MENSAGEM, EVENTO_PAR_DESCONECTADO
from jogo_p2p import JogoP2P, AGUARDANDO_ACAO, AGUARDANDO_POS_COMPRA, ENCERRADA
from tests.helpers import paralelo, estado


def servir_lobby(canal):
    servidor = ServidorLobby('127.0.0.1', 0)
    servidor.iniciar()
    canal.send(servidor.endereco)
    canal.close()
    servidor.servir_para_sempre()


def fechar_rede(rede):
    # shutdown acorda accept() também em sistemas onde close() sozinho não acorda.
    rede._rodando.clear()
    if rede._sock_servidor is not None:
        try:
            rede._sock_servidor.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    rede.fechar()


class IntegracaoTests(unittest.TestCase):
    def setUp(self):
        contexto = multiprocessing.get_context('spawn')
        receptor, emissor = contexto.Pipe(duplex=False)
        self.addCleanup(receptor.close)
        self.processo = contexto.Process(target=servir_lobby, args=(emissor,), daemon=True)
        self.processo.start()
        emissor.close()
        self.addCleanup(self.parar_lobby)
        self.assertTrue(receptor.poll(5), 'Lobby não iniciou em 5 segundos')
        self.endereco = receptor.recv()

    def parar_lobby(self):
        if self.processo.is_alive():
            self.processo.terminate()
        self.processo.join(timeout=3)
        if self.processo.is_alive():
            self.processo.kill()
            self.processo.join(timeout=3)
        self.assertFalse(self.processo.is_alive(), 'Processo do lobby não encerrou')

    def cliente(self, nome=None, porta=6001):
        cliente = ClienteLobby(*self.endereco)
        self.addCleanup(cliente.fechar)
        pid = cliente.registrar(nome, porta) if nome else None
        return cliente, pid

    def evento(self, origem, predicado, timeout=3):
        prazo = time.monotonic()+timeout
        while time.monotonic() < prazo:
            evento = origem.proximo_evento(timeout=min(0.1, prazo-time.monotonic()))
            if evento is not None and predicado(evento):
                return evento
        self.fail('Evento esperado não chegou no prazo')

    def erro(self, codigo, fn, *args):
        with self.assertRaises(ErroLobby) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.codigo, codigo)

    def test_registro_unico_validacao_e_estado(self):
        a, pid = self.cliente('Ana')
        b, _ = self.cliente()
        self.erro(p.E_ESTADO_INVALIDO, b.listar_partidas)
        self.erro(p.E_NOME_EM_USO, b.registrar, 'aNA', 6002)
        self.erro(p.E_CAMPO_INVALIDO, b.registrar, 'Bia', 0)
        self.assertNotEqual(b.registrar('Bia', 6002), pid)
        self.assertEqual(a.listar_partidas(), [])

    def test_sala_capacidade_autorizacao_e_notificacoes(self):
        a, aid = self.cliente('Ana')
        b, bid = self.cliente('Bia')
        c, _ = self.cliente('Caio')
        d, _ = self.cliente('Davi')
        self.erro(p.E_CAMPO_INVALIDO, a.criar_partida, 'Sala', 2)
        sala = a.criar_partida('Sala', 3)
        sid = sala['id_partida']
        self.assertEqual(a.listar_partidas()[0]['num_jogadores'], 1)
        self.erro(p.E_JOGADORES_INSUFICIENTES, a.iniciar_partida)
        b.entrar_partida(sid)
        self.erro(p.E_NAO_AUTORIZADO, b.iniciar_partida)
        c.entrar_partida(sid)
        self.erro(p.E_PARTIDA_CHEIA, d.entrar_partida, sid)
        self.erro(p.E_PARTIDA_INEXISTENTE, d.entrar_partida, sid+100)
        a.sair_partida()
        evento = self.evento(b, lambda e: e.get('partida', {}).get('criador') == bid)
        self.assertEqual(evento['tipo'], p.ATUALIZACAO_PARTIDA)
        self.assertEqual(evento['partida']['num_jogadores'], 2)
        b.sair_partida()
        c.sair_partida()
        self.assertEqual(d.listar_partidas(), [])

    def test_queda_cliente_transfere_criador(self):
        a, _ = self.cliente('Ana')
        b, bid = self.cliente('Bia')
        sid = a.criar_partida('Sala')['id_partida']
        b.entrar_partida(sid)
        a.fechar()
        evento = self.evento(b, lambda e: e.get('partida', {}).get('criador') == bid)
        self.assertEqual(evento['partida']['num_jogadores'], 1)

    def montar_partida(self, n=3):
        redes, clientes, ids = [], [], []
        for i in range(n):
            rede = RedeP2P('127.0.0.1', 0, intervalo_ping=0.1, timeout_par=3)
            self.addCleanup(fechar_rede, rede)
            porta = rede.iniciar_servidor()
            cliente, pid = self.cliente(f'Jogador {i}', porta)
            redes.append(rede)
            clientes.append(cliente)
            ids.append(pid)
        sid = clientes[0].criar_partida('Integração', n)['id_partida']
        for cliente in clientes[1:]:
            cliente.entrar_partida(sid)
        clientes[0].iniciar_partida()
        eventos = [self.evento(c, lambda e: e['tipo'] == p.PARTIDA_INICIADA) for c in clientes]
        self.assertTrue(all(e == eventos[0] for e in eventos))
        inicio = eventos[0]
        self.assertEqual(len(bytes.fromhex(inicio['chave_partida'])), 16)
        self.assertEqual([j['id_jogador'] for j in inicio['jogadores']], sorted(ids))
        self.assertEqual(clientes[0].listar_partidas(), [])
        resultados = paralelo([
            lambda rede=rede, pid=pid: rede.conectar_partida(
                pid, sid, inicio['chave_partida'], inicio['jogadores'], timeout_malha=4)
            for rede, pid in zip(redes, ids)
        ], timeout=6)
        for pid, (conectados, faltantes) in zip(ids, resultados):
            self.assertEqual(conectados, sorted(set(ids)-{pid}))
            self.assertEqual(faltantes, [])
        jogos = [JogoP2P(pid, inicio['jogadores'], rede) for pid, rede in zip(ids, redes)]
        # Sementes fixas só nos testes, para reprodução do baralho e dos jogadores mockados (bots).
        # Prazo também dentro do Commit-Reveal, cujo laço de produção não tem limite.
        prazo = time.monotonic()+20
        for rede in redes:
            original = rede.proximo_evento
            def limitado(timeout=None, original=original):
                if time.monotonic() > prazo:
                    raise AssertionError('Partida excedeu o prazo de teste')
                return original(timeout)
            substituicao = patch.object(rede, 'proximo_evento', side_effect=limitado)
            substituicao.start()
            self.addCleanup(substituicao.stop)
        with patch('jogo_p2p.secrets.token_hex', return_value='0123456789abcdef'*2):
            self.assertEqual(paralelo([j.iniciar for j in jogos]), [True]*n)
        self.assertTrue(all(estado(j) == estado(jogos[0]) for j in jogos))
        self.assertEqual([len(m) for m in jogos[0].maos.values()], [7]*n)
        self.assertTrue(jogos[0].topo_descarte[1].isdigit())
        return jogos, redes, clientes, inicio

    def jogar_ate_fim(self, jogos):
        prazo = time.monotonic()+15
        def bot(jogo):
            while jogo.estado != ENCERRADA:
                self.assertLess(time.monotonic(), prazo, 'Bot não terminou a partida')
                self.assertLess(jogo.seq, 2000, 'Limite de ações excedido')
                jogo.processar_eventos_pendentes()
                if jogo.eh_minha_vez():
                    if jogo.estado == AGUARDANDO_POS_COMPRA:
                        jogo.jogar_carta_comprada()
                    else:
                        validas = regras.cartas_validas(jogo.minha_mao, jogo.topo_descarte)
                        if validas:
                            jogo.jogar_carta(validas[0])
                        else:
                            jogo.comprar_carta()
                else:
                    jogo.aguardar_evento(timeout=0.05)
            return estado(jogo)
        with patch('jogo_p2p.secrets.token_hex', return_value='fedcba9876543210'*2):
            estados = paralelo([lambda j=j: bot(j) for j in jogos], timeout=17)
        self.assertTrue(all(e == estados[0] for e in estados), 'Réplicas divergiram')
        self.assertIsNotNone(jogos[0].vencedor)
        self.assertEqual(jogos[0].maos[jogos[0].vencedor], [])
        self.assertEqual(Counter(jogos[0].baralho+jogos[0].pilha_descarte+sum(jogos[0].maos.values(), [])),
                         Counter(baralho.criar_baralho_canonico()))

    def test_partida_completa_tres_jogadores(self):
        jogos, _, _, _ = self.montar_partida(3)
        self.jogar_ate_fim(jogos)

    def test_partida_quatro_jogadores_continua_apos_matar_lobby(self):
        jogos, _, clientes, _ = self.montar_partida(4)
        self.parar_lobby()  # encerra o processo e todos os sockets do servidor
        for cliente in clientes:
            self.evento(cliente, lambda e: e['tipo'] == LOBBY_DESCONECTADO)
            self.assertFalse(cliente.conectado)
        self.jogar_ate_fim(jogos)

    def test_reciclagem_commit_reveal_real_em_todos_os_pares(self):
        jogos, _, _, _ = self.montar_partida()
        for j in jogos:
            j.baralho = []
            j.pilha_descarte = [('Verde', '8'), ('Amarelo', '7'), ('Azul', '0')]
            j.maos[1] = [('Vermelho', '9')]
        def acao(j):
            if j.meu_id == 1:
                j.comprar_carta()
            else:
                while j.seq < 1:
                    j.aguardar_evento(0.1)
        with patch('jogo_p2p.secrets.token_hex', return_value='reciclagem'):
            paralelo([lambda j=j: acao(j) for j in jogos])
        self.assertTrue(all(estado(j) == estado(jogos[0]) for j in jogos))
        self.assertTrue(all(j.rodada_embaralhamento == 2 for j in jogos))
        self.assertEqual(jogos[0].pilha_descarte, [('Azul', '0')])
        self.assertEqual(Counter(jogos[0].baralho+jogos[0].maos[1][1:]),
                         Counter([('Verde', '8'), ('Amarelo', '7')]))

    def test_saida_voluntaria_e_fechamento_abrupto(self):
        jogos, redes, _, _ = self.montar_partida()
        redes[0].desconectar_partida()
        for j in jogos[1:]:
            evento = self.evento(j.rede, lambda e: e['evento'] == EVENTO_PAR_DESCONECTADO)
            self.assertEqual(evento['id_jogador'], 1)
            self.assertIn('TCHAU', evento['motivo'])
            j._processar_evento(evento)
            self.assertEqual(j.jogador_atual_id, 2)
        self.assertEqual(estado(jogos[1]), estado(jogos[2]))
        # Fecha socket sem enviar TCHAU: exercita EOF na conexão TCP real.
        redes[1]._pares[3].fechar()
        evento = self.evento(redes[2], lambda e: e['evento'] == EVENTO_PAR_DESCONECTADO)
        self.assertEqual(evento['id_jogador'], 2)
        jogos[2]._processar_evento(evento)
        self.assertEqual(jogos[2].estado, ENCERRADA)
        self.assertEqual(jogos[2].vencedor, 3)

    def test_handshake_rejeita_chave_partida_e_par_duplicado(self):
        _, redes, _, inicio = self.montar_partida()
        for mudanca, codigo in [({'chave_partida': 'errada'}, p.E_CHAVE_INVALIDA),
                                ({'id_partida': -1}, p.E_CHAVE_INVALIDA),
                                ({}, p.E_NAO_AUTORIZADO)]:
            with self.subTest(mudanca=mudanca):
                sock = socket.create_connection(('127.0.0.1', redes[0].porta_escuta), timeout=2)
                conexao = p.Conexao(sock)
                try:
                    campos = dict(id_jogador=2, id_partida=inicio['id_partida'],
                                  chave_partida=inicio['chave_partida'])
                    campos.update(mudanca)
                    conexao.enviar(p.OLA, **campos)
                    resposta = conexao.receber()
                    self.assertEqual(resposta['tipo'], p.ERRO)
                    self.assertEqual(resposta['codigo'], codigo)
                    with self.assertRaises(p.ConexaoFechada):
                        conexao.receber()
                finally:
                    conexao.fechar()
        self.assertEqual(redes[0].pares_conectados(), [2, 3])


class CLITests(unittest.TestCase):
    def test_ajuda_dos_pontos_de_entrada(self):
        raiz = Path(__file__).resolve().parents[1]
        for script, opcao in [('main.py', '--lobby-host'), ('servidor_lobby.py', '--porta')]:
            with self.subTest(script=script):
                resultado = subprocess.run([sys.executable, str(raiz/script), '--help'],
                                           capture_output=True, text=True, timeout=5)
                self.assertEqual(resultado.returncode, 0, resultado.stderr)
                self.assertIn(opcao, resultado.stdout)


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.rede = RedeP2P('127.0.0.1', 0, intervalo_ping=0.05, timeout_par=0.5)
        self.addCleanup(fechar_rede, self.rede)
        porta = self.rede.iniciar_servidor()
        jogadores = [dict(id_jogador=1, host='127.0.0.1', porta=porta),
                     dict(id_jogador=2, host='127.0.0.1', porta=1)]
        self.rede.conectar_partida(1, 1, 'segredo', jogadores, timeout_malha=0)
        self.conexao = p.Conexao(socket.create_connection(('127.0.0.1', porta), timeout=2))
        self.addCleanup(self.conexao.fechar)
        self.conexao.enviar(p.OLA, id_jogador=2, id_partida=1, chave_partida='segredo')
        self.assertEqual(self.conexao.receber()['tipo'], p.OLA_OK)

    def test_ping_periodico_e_pong_nao_chegam_ao_jogo(self):
        self.assertEqual(self.conexao.receber()['tipo'], p.PING)
        self.conexao.enviar(p.PONG)
        self.conexao.enviar(p.PING)
        prazo = time.monotonic()+2
        while time.monotonic() < prazo:
            msg = self.conexao.receber()
            if msg['tipo'] == p.PONG:
                break
            self.assertEqual(msg['tipo'], p.PING)
            self.conexao.enviar(p.PONG)
        else:
            self.fail('PING não recebeu PONG')
        # Mensagem posterior funciona como barreira no mesmo fluxo TCP.
        self.conexao.enviar(p.ACAO_PASSAR, seq=1)
        evento = self.rede.proximo_evento(timeout=2)
        self.assertIsNotNone(evento)
        self.assertEqual(evento['evento'], EVENTO_MENSAGEM)
        self.assertEqual(evento['msg'], dict(tipo=p.ACAO_PASSAR, seq=1))
        self.assertIsNone(self.rede.proximo_evento(timeout=0))

    def test_par_silencioso_expira_por_timeout(self):
        evento = self.rede.proximo_evento(timeout=3)
        self.assertIsNotNone(evento)
        self.assertEqual(evento['evento'], EVENTO_PAR_DESCONECTADO)
        self.assertEqual(evento['id_jogador'], 2)
        self.assertIn('timeout', evento['motivo'])
        self.assertEqual(self.rede.pares_conectados(), [])
        self.assertIsNone(self.rede.proximo_evento(timeout=0))
