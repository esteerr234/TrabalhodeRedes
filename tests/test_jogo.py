import hashlib
import unittest
from collections import Counter
from unittest.mock import patch

import baralho
import protocolo as p
import rede_p2p as r
from jogo_p2p import AGUARDANDO_ACAO, AGUARDANDO_POS_COMPRA, ENCERRADA
from tests.helpers import jogo_pronto, estado


class JogoTests(unittest.TestCase):
    def setUp(self):
        self.jogo = jogo_pronto()

    def rejeitada(self, remetente, codigo, **msg):
        antes = estado(self.jogo)
        self.jogo._tratar_mensagem(remetente, msg)
        self.assertEqual(estado(self.jogo), antes)
        self.assertEqual(self.jogo.rede.enviar_para.call_args.args[:2], (remetente, p.ERRO))
        self.assertEqual(self.jogo.rede.enviar_para.call_args.kwargs['codigo'], codigo)

    def test_jogada_local_emite_sequencia_e_avanca(self):
        self.jogo.jogar_carta(('Azul', '1'))
        self.jogo.rede.broadcast.assert_called_once_with(p.ACAO_JOGAR, seq=1, carta=['Azul', '1'])
        self.assertEqual(self.jogo.jogador_atual_id, 2)
        self.assertEqual(self.jogo.minha_mao, [('Verde', '2')])
        self.assertEqual(self.jogo.topo_descarte, ('Azul', '1'))

    def test_validacoes_locais_nao_emitem_nem_alteram_estado(self):
        for acao in (lambda: self.jogo.jogar_carta(('Azul', '9')),
                     lambda: self.jogo.jogar_carta(('Verde', '2')),
                     self.jogo.comprar_carta, self.jogo.passar_vez,
                     self.jogo.jogar_carta_comprada):
            with self.subTest(acao=acao):
                antes = estado(self.jogo)
                with self.assertRaises(ValueError):
                    acao()
                self.assertEqual(estado(self.jogo), antes)
        self.jogo.rede.broadcast.assert_not_called()

    def test_jogada_fora_de_turno(self):
        self.rejeitada(2, p.E_FORA_DE_TURNO, tipo=p.ACAO_JOGAR, seq=1, carta=['Azul', '3'])

    def test_cartas_invalidas(self):
        for carta, codigo in [(['Azul', '9'], p.E_JOGADA_INVALIDA),
                              (['Verde', '2'], p.E_JOGADA_INVALIDA),
                              (['Azul'], p.E_CAMPO_INVALIDO)]:
            with self.subTest(carta=carta):
                self.rejeitada(1, codigo, tipo=p.ACAO_JOGAR, seq=1, carta=carta)

    def test_compra_com_carta_valida_e_passe_prematuro(self):
        self.rejeitada(1, p.E_JOGADA_INVALIDA, tipo=p.ACAO_COMPRAR, seq=1)
        self.rejeitada(1, p.E_ESTADO_INVALIDO, tipo=p.ACAO_PASSAR, seq=1)

    def test_tipo_desconhecido(self):
        self.rejeitada(1, p.E_TIPO_DESCONHECIDO, tipo='INVENTADO')

    def test_efeitos_especiais(self):
        for valor, proximo, sentido, tamanho in [('Pular', 3, 1, 2),
                                                ('Inverter', 3, -1, 2),
                                                ('+2', 3, 1, 4)]:
            with self.subTest(valor=valor):
                j = jogo_pronto()
                j.maos[1][0] = ('Azul', valor)
                j.jogar_carta(('Azul', valor))
                self.assertEqual(j.jogador_atual_id, proximo)
                self.assertEqual(j.sentido, sentido)
                self.assertEqual(len(j.maos[2]), tamanho)
                if valor == '+2':
                    self.assertEqual(j.maos[2][-2:], [('Azul', '7'), ('Amarelo', '9')])

    def test_ultima_carta_declara_vencedor(self):
        self.jogo.maos[1] = [('Azul', '1')]
        self.jogo.jogar_carta(('Azul', '1'))
        self.assertEqual(self.jogo.estado, ENCERRADA)
        self.assertEqual(self.jogo.vencedor, 1)
        antes = estado(self.jogo)
        self.jogo._tratar_mensagem(2, dict(tipo=p.ACAO_JOGAR, seq=2, carta=['Azul', '3']))
        self.assertEqual(estado(self.jogo), antes)

    def preparar_compra(self, carta):
        self.jogo.maos[1] = [('Verde', '2')]
        self.jogo.baralho = [carta]
        return self.jogo.comprar_carta()

    def test_compra_incompativel_passa_automaticamente(self):
        self.assertEqual(self.preparar_compra(('Vermelho', '3')), ('Vermelho', '3'))
        self.assertEqual(self.jogo.jogador_atual_id, 2)
        self.assertEqual(self.jogo.estado, AGUARDANDO_ACAO)
        self.assertEqual(len(self.jogo.minha_mao), 2)

    def test_compra_jogavel_pode_ser_recusada(self):
        self.preparar_compra(('Azul', '3'))
        self.assertEqual(self.jogo.estado, AGUARDANDO_POS_COMPRA)
        self.assertEqual(self.jogo.jogador_atual_id, 1)
        self.jogo.passar_vez()
        self.assertEqual(self.jogo.jogador_atual_id, 2)
        self.assertEqual(self.jogo.seq, 2)
        self.assertIsNone(self.jogo.carta_comprada_pendente)
        self.assertIn(('Azul', '3'), self.jogo.minha_mao)

    def test_compra_jogavel_aplica_efeito(self):
        self.preparar_compra(('Azul', 'Pular'))
        self.jogo.jogar_carta_comprada()
        self.assertEqual(self.jogo.jogador_atual_id, 3)
        self.assertEqual(self.jogo.topo_descarte, ('Azul', 'Pular'))
        self.assertIsNone(self.jogo.carta_comprada_pendente)
        self.assertEqual(self.jogo.seq, 2)

    def test_pos_compra_so_permite_carta_comprada(self):
        self.preparar_compra(('Azul', '3'))
        self.rejeitada(1, p.E_JOGADA_INVALIDA, tipo=p.ACAO_JOGAR_COMPRADA,
                       seq=2, carta=['Verde', '2'])
        self.rejeitada(1, p.E_ESTADO_INVALIDO, tipo=p.ACAO_COMPRAR, seq=2)
        self.rejeitada(1, p.E_ESTADO_INVALIDO, tipo=p.ACAO_JOGAR, seq=2, carta=['Azul', '3'])

    def test_compra_sem_monte_nem_descarte_reciclavel(self):
        self.jogo.maos[1] = [('Verde', '2')]
        self.jogo.baralho = []
        self.assertIsNone(self.jogo.comprar_carta())
        self.assertEqual(self.jogo.jogador_atual_id, 2)
        self.assertEqual(len(self.jogo.minha_mao), 1)

    def test_reciclagem_preserva_topo_e_cartas(self):
        self.jogo.baralho = []
        self.jogo.pilha_descarte = [('Verde', '2'), ('Azul', '4'), ('Azul', '0')]
        with patch.object(self.jogo, '_executar_commit_reveal', return_value=42) as commit:
            carta = self.jogo._comprar_do_monte()
        commit.assert_called_once()
        self.assertEqual(self.jogo.pilha_descarte, [('Azul', '0')])
        self.assertEqual(Counter(self.jogo.baralho+[carta]), Counter([('Verde', '2'), ('Azul', '4')]))

    def test_acao_futura_aguarda_antecessora(self):
        antes = estado(self.jogo)
        self.jogo._tratar_mensagem(2, dict(tipo=p.ACAO_JOGAR, seq=2, carta=['Azul', '3']))
        self.assertEqual(estado(self.jogo), antes)
        self.jogo._tratar_mensagem(1, dict(tipo=p.ACAO_JOGAR, seq=1, carta=['Azul', '1']))
        self.assertEqual(self.jogo.seq, 2)
        self.assertEqual(self.jogo.topo_descarte, ('Azul', '3'))
        self.assertEqual(self.jogo.jogador_atual_id, 3)
        self.assertEqual(self.jogo._acoes_futuras, {})

    def test_sequencia_obsoleta_e_fora_da_janela(self):
        for seq in (0, 100):
            self.rejeitada(1, p.E_FORA_DE_TURNO, tipo=p.ACAO_JOGAR, seq=seq, carta=['Azul', '1'])

    def test_regressao_sequencia_obrigatoria_e_inteira(self):
        for tipo in (p.ACAO_JOGAR, p.ACAO_COMPRAR, p.ACAO_JOGAR_COMPRADA, p.ACAO_PASSAR):
            for campos in ({}, {'seq': None}, {'seq': True}, {'seq': False},
                           {'seq': '1'}, {'seq': 1.0}):
                with self.subTest(tipo=tipo, campos=campos):
                    self.jogo = jogo_pronto()
                    self.rejeitada(1, p.E_CAMPO_INVALIDO, tipo=tipo,
                                   carta=['Azul', '1'], **campos)
                    self.assertEqual(self.jogo._acoes_futuras, {})

    def test_desconexao_avanca_no_sentido_atual(self):
        for sentido, proximo in [(1, 2), (-1, 3)]:
            with self.subTest(sentido=sentido):
                j = jogo_pronto()
                j.sentido = sentido
                monte, descarte = j.baralho[:], j.pilha_descarte[:]
                j._tratar_desconexao(1, 'teste')
                self.assertEqual(j.jogador_atual_id, proximo)
                self.assertNotIn(1, j.maos)
                self.assertEqual(j.baralho, monte)
                self.assertEqual(j.pilha_descarte, descarte)
                antes = estado(j)
                j._tratar_desconexao(1, 'duplicada')
                self.assertEqual(estado(j), antes)

    def test_desconexao_de_outro_preserva_jogador_da_vez(self):
        self.jogo.idx_atual = 2
        self.jogo._tratar_desconexao(1, 'teste')
        self.assertEqual(self.jogo.jogador_atual_id, 3)

    def test_desconexao_pos_compra_limpa_pendencia(self):
        self.preparar_compra(('Azul', '3'))
        self.jogo._tratar_desconexao(1, 'teste')
        self.assertEqual(self.jogo.estado, AGUARDANDO_ACAO)
        self.assertIsNone(self.jogo.carta_comprada_pendente)

    def test_sobrevivente_vence(self):
        self.jogo._tratar_desconexao(2, 'teste')
        self.jogo._tratar_desconexao(3, 'teste')
        self.assertEqual(self.jogo.estado, ENCERRADA)
        self.assertEqual(self.jogo.vencedor, 1)


class CommitRevealTests(unittest.TestCase):
    def setUp(self):
        self.jogo = jogo_pronto()

    def mensagem(self, pid, tipo, **campos):
        return dict(evento=r.EVENTO_MENSAGEM, remetente=pid,
                    msg=dict(tipo=tipo, rodada=1, id_jogador=pid, **campos))

    def test_commit_reveal_combina_por_id_e_revela_apos_commits(self):
        eventos = []
        for pid in (3, 2):
            eventos.append(self.mensagem(pid, p.COMMIT_SEMENTE,
                           commit=hashlib.sha256(f's{pid}'.encode()).hexdigest()))
        for pid in (2, 3):
            eventos.append(self.mensagem(pid, p.REVELAR_SEMENTE, semente=f's{pid}'))

        def broadcast(tipo, **campos):
            if tipo == p.REVELAR_SEMENTE:
                self.assertEqual(set(self.jogo._commits[1]), {1, 2, 3})
        self.jogo.rede.broadcast.side_effect = broadcast
        self.jogo.rede.proximo_evento.side_effect = eventos
        with patch('jogo_p2p.secrets.token_hex', return_value='s1'):
            resultado = self.jogo._executar_commit_reveal()
        esperado = int(hashlib.sha256(b'1:s1|2:s2|3:s3').hexdigest(), 16)
        self.assertEqual(resultado, esperado)
        self.assertEqual(self.jogo.rodada_embaralhamento, 1)

    def test_revelacao_adiantada_validada_apos_commit(self):
        j = self.jogo
        j._registrar_msg_embaralhamento(2, dict(tipo=p.REVELAR_SEMENTE, rodada=1, semente='s2'))
        self.assertNotIn(2, j._revelacoes[1])
        j._registrar_msg_embaralhamento(2, dict(tipo=p.COMMIT_SEMENTE, rodada=1,
                                              commit=hashlib.sha256(b's2').hexdigest()))
        self.assertEqual(j._revelacoes[1][2], 's2')

    def test_semente_adulterada_rejeitada(self):
        j = self.jogo
        j._registrar_msg_embaralhamento(2, dict(tipo=p.COMMIT_SEMENTE, rodada=1,
                                              commit=hashlib.sha256(b'original').hexdigest()))
        j._registrar_msg_embaralhamento(2, dict(tipo=p.REVELAR_SEMENTE, rodada=1, semente='fraude'))
        self.assertNotIn(2, j._revelacoes[1])
        self.assertEqual(j.rede.enviar_para.call_args.kwargs['codigo'], p.E_SEMENTE_INVALIDA)

    def test_queda_desbloqueia_commit_reveal(self):
        self.jogo.rede.proximo_evento.side_effect = [
            dict(evento=r.EVENTO_PAR_DESCONECTADO, id_jogador=3, motivo='queda'),
            self.mensagem(2, p.COMMIT_SEMENTE, commit=hashlib.sha256(b's2').hexdigest()),
            self.mensagem(2, p.REVELAR_SEMENTE, semente='s2'),
        ]
        with patch('jogo_p2p.secrets.token_hex', return_value='s1'):
            resultado = self.jogo._executar_commit_reveal()
        self.assertEqual(resultado, int(hashlib.sha256(b'1:s1|2:s2').hexdigest(), 16))
        self.assertEqual(self.jogo.ativos, [1, 2])

    def test_inicio_distribui_e_conserva_baralho(self):
        j = self.jogo
        j.pilha_descarte = []
        with patch.object(j, '_executar_commit_reveal', return_value=123):
            self.assertTrue(j.iniciar())
        self.assertEqual([len(m) for m in j.maos.values()], [7, 7, 7])
        self.assertTrue(j.topo_descarte[1].isdigit())
        self.assertEqual(Counter(j.baralho+j.pilha_descarte+sum(j.maos.values(), [])),
                         Counter(baralho.criar_baralho_canonico()))
