import socket
import unittest
from unittest.mock import Mock

import protocolo as p


class ProtocoloTests(unittest.TestCase):
    def assertErro(self, codigo, fn, *args, **kwargs):
        with self.assertRaises(p.ErroProtocolo) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.codigo, codigo)

    def test_roundtrip_unicode_e_quebra_de_linha(self):
        dados = p.codificar('TESTE', texto='Érica\nOlá')
        self.assertEqual(dados.count(b'\n'), 1)
        self.assertEqual(p.decodificar(dados[:-1]), {'tipo': 'TESTE', 'texto': 'Érica\nOlá'})

    def test_json_invalido_e_tipo_obrigatorio(self):
        for dados in (b'\xff', b'{', b'[]', b'null', b'42'):
            with self.subTest(dados=dados):
                self.assertErro(p.E_JSON_INVALIDO, p.decodificar, dados)
        for dados in (b'{}', b'{"tipo":1}', b'{"tipo":null}'):
            with self.subTest(dados=dados):
                self.assertErro(p.E_CAMPO_INVALIDO, p.decodificar, dados)

    def test_limite_envio_inclui_delimitador_e_bytes_utf8(self):
        tamanho = p.TAM_MAX_MENSAGEM - len(p.codificar('X', texto=''))
        self.assertEqual(len(p.codificar('X', texto='a'*tamanho)), p.TAM_MAX_MENSAGEM)
        self.assertErro(p.E_MENSAGEM_GRANDE, p.codificar, 'X', texto='a'*(tamanho+1))
        self.assertErro(p.E_MENSAGEM_GRANDE, p.codificar, 'X', texto='á'*tamanho)

    def test_validacao_campos(self):
        self.assertEqual(p.campo_str({'x': ' Ana '}, 'x'), 'Ana')
        self.assertEqual(p.campo_int({'x': 3}, 'x', 3, 4), 3)
        self.assertEqual(p.campo_carta({'carta': ['Azul', '1']}), ('Azul', '1'))
        for valor in (True, False, '3', None, 2.5, 0, 5):
            with self.subTest(valor=valor):
                self.assertErro(p.E_CAMPO_INVALIDO, p.campo_int, {'x': valor}, 'x', 1, 4)
        for valor in (' ', '', None, 123, 'x'*33):
            self.assertErro(p.E_CAMPO_INVALIDO, p.campo_str, {'x': valor}, 'x')
        for valor in (None, 'Azul', ['Azul'], ['Azul', 1], ['Azul', '1', 'x']):
            self.assertErro(p.E_CAMPO_INVALIDO, p.campo_carta, {'carta': valor})

    def conexao_fragmentos(self, partes):
        sock = Mock()
        sock.recv.side_effect = partes
        return p.Conexao(sock)

    def test_fragmentacao_inclusive_no_meio_de_utf8(self):
        dados = p.codificar('TESTE', texto='ação')
        c = self.conexao_fragmentos([bytes([b]) for b in dados])
        self.assertEqual(c.receber()['texto'], 'ação')

    def test_mensagens_agrupadas_e_crlf(self):
        c = self.conexao_fragmentos([b'{"tipo":"A"}\r\n{"tipo":"B"}\n'])
        self.assertEqual(c.receber(), {'tipo': 'A'})
        self.assertEqual(c.receber(), {'tipo': 'B'})
        c.sock.recv.assert_called_once()

    def test_linha_invalida_nao_descarta_proxima(self):
        c = self.conexao_fragmentos([b'lixo\n'+p.codificar(p.PING)])
        self.assertErro(p.E_JSON_INVALIDO, c.receber)
        self.assertEqual(c.receber()['tipo'], p.PING)

    def test_eof_e_falha_de_envio(self):
        c = self.conexao_fragmentos([b''])
        with self.assertRaises(p.ConexaoFechada):
            c.receber()
        c.sock.sendall.side_effect = OSError('fechado')
        with self.assertRaises(p.ConexaoFechada):
            c.enviar(p.PING)
        c.enviar_erro('ERRO')  # envio de erro não propaga desconexão

    def test_timeout_preserva_fragmento(self):
        c = self.conexao_fragmentos([b'{"tipo":', socket.timeout(), b'"PING"}\n'])
        with self.assertRaises(socket.timeout):
            c.receber()
        self.assertEqual(c.receber()['tipo'], p.PING)

    def test_fechar_idempotente(self):
        c = self.conexao_fragmentos([])
        c.fechar()
        c.fechar()
        self.assertTrue(c.fechada)
        c.sock.close.assert_called_once()
        with self.assertRaises(p.ConexaoFechada):
            c.enviar(p.PING)

    def test_rejeita_linha_grande_sem_delimitador(self):
        c = self.conexao_fragmentos([b'x'*4096]*17)
        self.assertErro(p.E_MENSAGEM_GRANDE, c.receber)

    def test_regressao_limite_recebimento_com_delimitador(self):
        # O último bloco ultrapassa o limite e traz o delimitador.
        dados = b'{"tipo":"X","texto":"' + b'a'*p.TAM_MAX_MENSAGEM + b'"}\n'
        partes = [dados[i:i+4096] for i in range(0, len(dados), 4096)]
        c = self.conexao_fragmentos(partes)
        self.assertErro(p.E_MENSAGEM_GRANDE, c.receber)

    def test_recebimento_no_limite_e_recuperacao_apos_excesso(self):
        tamanho = p.TAM_MAX_MENSAGEM - len(p.codificar('X', texto=''))
        dados = p.codificar('X', texto='a'*tamanho)
        partes = [dados[i:i+4096] for i in range(0, len(dados), 4096)]
        c = self.conexao_fragmentos(partes)
        self.assertEqual(c.receber()['texto'], 'a'*tamanho)
        # Exatamente um byte acima do limite, seguido de mensagem válida.
        excesso = dados[:-1] + b' \n' + p.codificar(p.PING)
        partes = [excesso[i:i+4096] for i in range(0, len(excesso), 4096)]
        c = self.conexao_fragmentos(partes)
        self.assertErro(p.E_MENSAGEM_GRANDE, c.receber)
        self.assertEqual(c.receber(), {'tipo': p.PING})
