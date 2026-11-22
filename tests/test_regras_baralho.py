import contextlib
import io
import random
import unittest
from collections import Counter
from unittest.mock import patch

import baralho
import regras
import Jogador


class BaralhoRegrasTests(unittest.TestCase):
    def test_baralho_canonico_tem_104_cartas_duas_de_cada(self):
        esperado = Counter({(cor, valor): 2
                            for cor in ('Vermelho', 'Amarelo', 'Verde', 'Azul')
                            for valor in [str(n) for n in range(10)] + ['Pular', 'Inverter', '+2']})
        cartas = baralho.criar_baralho_canonico()
        self.assertEqual(len(cartas), 104)
        self.assertEqual(Counter(cartas), esperado)
        self.assertEqual(cartas, baralho.criar_baralho_canonico())

    def test_embaralhamento_deterministico_preserva_cartas_e_rng_global(self):
        a, b = baralho.criar_baralho_canonico(), baralho.criar_baralho_canonico()
        antes = random.getstate()
        self.assertIs(baralho.embaralhar_deterministico(a, 12345), a)
        baralho.embaralhar_deterministico(b, 12345)
        self.assertEqual(a, b)
        self.assertNotEqual(a, baralho.criar_baralho_canonico())
        self.assertEqual(Counter(a), Counter(baralho.criar_baralho_canonico()))
        self.assertEqual(random.getstate(), antes)

    def test_distribuicao_sete_cartas_sem_perdas(self):
        for n in (3, 4):
            with self.subTest(jogadores=n):
                cartas = baralho.criar_baralho_canonico()
                maos = baralho.distribuir_cartas(cartas, n)
                self.assertEqual([len(m) for m in maos], [7]*n)
                self.assertEqual(len(cartas), 104-7*n)
                self.assertEqual(Counter(cartas + sum(maos, [])),
                                 Counter(baralho.criar_baralho_canonico()))

    def test_compra_legada_recicla_preservando_topo(self):
        monte, descarte = [], [('Azul', '1'), ('Verde', '2'), ('Amarelo', '3')]
        with patch('baralho.random.shuffle'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(baralho.comprar_carta(monte, descarte), ('Verde', '2'))
        self.assertEqual(descarte, [('Amarelo', '3')])
        self.assertEqual(monte, [('Azul', '1')])

    def test_compra_sem_cartas_retorna_none(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(baralho.comprar_carta([], [('Azul', '1')]))

    def test_jogada_por_cor_ou_valor(self):
        for carta, topo, esperado in [
            (('Azul', '1'), ('Azul', '9'), True),
            (('Verde', '1'), ('Azul', '1'), True),
            (('Verde', '+2'), ('Azul', '+2'), True),
            (('Verde', '2'), ('Azul', '1'), False),
        ]:
            with self.subTest(carta=carta, topo=topo):
                self.assertEqual(regras.pode_jogar(carta, topo), esperado)

    def test_cartas_validas_preservam_duplicatas_e_mao(self):
        mao = [('Azul', '1'), ('Verde', '2'), ('Azul', '1')]
        antes = mao[:]
        self.assertEqual(regras.cartas_validas(mao, ('Azul', '9')), [mao[0], mao[2]])
        self.assertEqual(mao, antes)

    def test_efeitos_nos_dois_sentidos(self):
        for sentido in (-1, 1):
            for valor, esperado in [('0', (0, False, sentido)),
                                    ('+2', (2, True, sentido)),
                                    ('Pular', (0, True, sentido)),
                                    ('Inverter', (0, False, -sentido))]:
                with self.subTest(valor=valor, sentido=sentido), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(regras.aplicar_efeito(('Azul', valor), sentido), esperado)

    def test_turno_circular(self):
        self.assertEqual(regras.proximo_jogador(2, 3, 1), 0)
        self.assertEqual(regras.proximo_jogador(0, 4, -1), 3)

    def test_jogadores_nao_compartilham_mao(self):
        a, b = Jogador.criar_jogador('Ana'), Jogador.criar_jogador('Bia')
        a['mao'].append(('Azul', '1'))
        self.assertEqual(b, dict(nome='Bia', mao=[], conectado=True))
