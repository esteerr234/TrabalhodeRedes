"""
Ponto de entrada CLI do jogador (Lobby + Partida P2P).

Uso:
    python3 main.py --nome Ana --porta-p2p 6001 --lobby-host 127.0.0.1 --lobby-porta 5000
"""

import argparse
import logging
import select
import sys

import cliente_lobby as cl
import jogo_p2p
import protocolo as p
import rede_p2p
import regras


def _ler_linha_com_timeout(timeout=0.25):
    """Lê uma linha de stdin sem bloquear indefinidamente (para reagir a eventos do lobby)."""
    try:
        pronto, _, _ = select.select([sys.stdin], [], [], timeout)
        if pronto:
            linha = sys.stdin.readline()
            if linha == "":
                return None
            return linha.strip()
    except (OSError, ValueError):
        pass
    return None


def _mostrar_estado_mesa(jogo):
    sentido_txt = "horário" if jogo.sentido == 1 else "anti-horário"
    print("\n========================================")
    print(f"Carta na mesa: {jogo.topo_descarte} | Sentido: {sentido_txt}")
    print(f"Monte de compra: {len(jogo.baralho)} carta(s)")
    resumo_jogadores = ", ".join(
        f"{jogo.nome_jogador(pid)} ({len(jogo.maos[pid])})"
        + (" *" if pid == jogo.jogador_atual_id else "")
        for pid in jogo.ativos
    )
    print(f"Jogadores ativos: {resumo_jogadores}")
    print("========================================")


def executar_partida_p2p(meu_id, evento_inicio, rede):
    """Conecta à malha P2P e executa o loop interativo da partida até o fim."""
    id_partida = evento_inicio["id_partida"]
    chave_partida = evento_inicio["chave_partida"]
    jogadores = evento_inicio["jogadores"]

    print(f"\n>>> Partida {id_partida} iniciada! Conectando aos pares P2P...")
    conectados, faltantes = rede.conectar_partida(
        meu_id=meu_id,
        id_partida=id_partida,
        chave_partida=chave_partida,
        jogadores=jogadores,
    )

    if faltantes:
        print(f"[AVISO] Não foi possível conectar aos pares: {faltantes}")

    jogadores_ativos = [
        j for j in jogadores if j["id_jogador"] == meu_id or j["id_jogador"] in conectados
    ]
    if len(jogadores_ativos) < 2:
        print("[ERRO] Pares insuficientes para iniciar a partida.")
        rede.desconectar_partida()
        return

    jogo = jogo_p2p.JogoP2P(
        meu_id=meu_id,
        jogadores=jogadores_ativos,
        rede=rede,
        on_evento=lambda texto: print(f"[JOGO] {texto}"),
    )

    if not jogo.iniciar():
        print(f"\nPartida encerrada: {jogo.motivo_encerramento}")
        rede.desconectar_partida()
        return

    ultimo_aguardado = None

    try:
        while jogo.estado != jogo_p2p.ENCERRADA:
            jogo.processar_eventos_pendentes()
            if jogo.estado == jogo_p2p.ENCERRADA:
                break

            if jogo.eh_minha_vez():
                ultimo_aguardado = None
                _mostrar_estado_mesa(jogo)
                print(f"\n>>> É A SUA VEZ, {jogo.nome_jogador(meu_id)}!")

                if jogo.estado == jogo_p2p.AGUARDANDO_ACAO:
                    print("\nSua mão:")
                    for idx, carta in enumerate(jogo.minha_mao, start=1):
                        print(f"  {idx} - {carta}")

                    validas = regras.cartas_validas(jogo.minha_mao, jogo.topo_descarte)
                    if not validas:
                        print("\nVocê não possui cartas válidas. Comprando 1 carta...")
                        jogo.comprar_carta()
                        continue

                    print("\nCartas válidas na sua mão:", validas)
                    while jogo.estado == jogo_p2p.AGUARDANDO_ACAO and jogo.eh_minha_vez():
                        escolha = input("Digite o número da carta que deseja jogar: ").strip()
                        jogo.processar_eventos_pendentes()
                        if jogo.estado == jogo_p2p.ENCERRADA or not jogo.eh_minha_vez():
                            break
                        try:
                            indice = int(escolha) - 1
                            if indice < 0 or indice >= len(jogo.minha_mao):
                                print("Número inválido!")
                                continue
                            carta = jogo.minha_mao[indice]
                            if not regras.pode_jogar(carta, jogo.topo_descarte):
                                print("Essa carta não pode ser jogada!")
                                continue
                            jogo.jogar_carta(carta)
                            break
                        except ValueError:
                            print("Digite um número válido!")

                elif jogo.estado == jogo_p2p.AGUARDANDO_POS_COMPRA:
                    carta_comp = jogo.carta_comprada_pendente
                    while jogo.estado == jogo_p2p.AGUARDANDO_POS_COMPRA and jogo.eh_minha_vez():
                        resp = input(
                            f"A carta comprada {carta_comp} pode ser jogada! Deseja jogar? (s/n): "
                        ).strip().lower()
                        jogo.processar_eventos_pendentes()
                        if jogo.estado == jogo_p2p.ENCERRADA or not jogo.eh_minha_vez():
                            break
                        if resp == "s":
                            jogo.jogar_carta_comprada()
                            break
                        elif resp == "n":
                            jogo.passar_vez()
                            break
                        else:
                            print("Responda com 's' ou 'n'.")

            else:
                atual = jogo.jogador_atual_id
                if atual != ultimo_aguardado:
                    _mostrar_estado_mesa(jogo)
                    print(f"\nAguardando jogada de {jogo.nome_jogador(atual)}...")
                    ultimo_aguardado = atual
                jogo.aguardar_evento(timeout=0.3)

    except KeyboardInterrupt:
        print("\nSaindo da partida (enviando TCHAU aos pares)...")
    finally:
        print("\n========================================")
        if jogo.vencedor is not None:
            print(f"PARTIDA ENCERRADA — Vencedor: {jogo.nome_jogador(jogo.vencedor)}")
            if jogo.motivo_encerramento:
                print(jogo.motivo_encerramento)
        else:
            print("PARTIDA ENCERRADA")
        print("========================================\n")
        rede.desconectar_partida()


def aguardar_na_sala(lobby, meu_id, partida):
    """
    Aguarda na sala do lobby até PARTIDA_INICIADA, saída voluntária ou queda do lobby.
    Retorna o dict do evento PARTIDA_INICIADA ou None se saiu da sala.
    """
    def _mostrar_sala(pt):
        nomes = ", ".join(j["nome"] for j in pt["jogadores"])
        sou_criador = pt["criador"] == meu_id
        print(
            f"\n[SALA #{pt['id_partida']} '{pt['nome']}'] "
            f"{pt['num_jogadores']}/{pt['max_jogadores']} jogadores: {nomes}"
        )
        if sou_criador:
            print("Você é o criador. Digite 'iniciar' (mín. 3 jogadores) ou 'sair':")
        else:
            print("Aguardando o criador iniciar a partida (digite 'sair' para sair da sala)...")

    _mostrar_sala(partida)

    while True:
        evento = lobby.proximo_evento(timeout=0.1)
        if evento is not None:
            tipo = evento["tipo"]
            if tipo == p.ATUALIZACAO_PARTIDA:
                partida = evento["partida"]
                _mostrar_sala(partida)
                continue
            if tipo == p.PARTIDA_INICIADA:
                return evento
            if tipo == cl.LOBBY_DESCONECTADO:
                print("\n[AVISO] Conexão com o servidor de lobby foi perdida.")
                return None

        cmd = _ler_linha_com_timeout(timeout=0.2)
        if cmd is None:
            continue
        cmd_lower = cmd.lower()
        if cmd_lower in ("sair", "s"):
            try:
                lobby.sair_partida()
                print("Você saiu da sala.")
            except cl.ErroLobby as e:
                print(f"Erro ao sair da sala: {e}")
            return None
        elif cmd_lower in ("iniciar", "i"):
            try:
                lobby.iniciar_partida()
                print("Início solicitado! Aguardando notificação PARTIDA_INICIADA...")
            except cl.ErroLobby as e:
                print(f"Não foi possível iniciar: {e}")
        elif cmd_lower:
            print("Comando inválido. Use 'iniciar' (se criador) ou 'sair'.")


def menu_lobby(lobby, meu_id, rede):
    """Menu interativo do lobby para listar, criar e entrar em partidas."""
    while lobby.conectado:
        print("\n=== MENU DO LOBBY ===")
        print("1 - Listar partidas")
        print("2 - Criar partida")
        print("3 - Entrar em partida")
        print("0 - Sair")
        opcao = input("Escolha: ").strip()

        if opcao == "1":
            try:
                partidas = lobby.listar_partidas()
                if not partidas:
                    print("\nNenhuma partida aguardando jogadores.")
                else:
                    print("\nPartidas disponíveis:")
                    for pt in partidas:
                        print(
                            f"  #{pt['id_partida']} - {pt['nome']} "
                            f"({pt['num_jogadores']}/{pt['max_jogadores']}) [{pt['estado']}]"
                        )
            except cl.ErroLobby as e:
                print(f"Erro ao listar partidas: {e}")

        elif opcao == "2":
            nome_partida = input("Nome da partida: ").strip() or "Mesa Uno"
            max_str = input("Máximo de jogadores (3 ou 4, padrão 4): ").strip() or "4"
            try:
                max_j = int(max_str)
                partida = lobby.criar_partida(nome_partida, max_jogadores=max_j)
                evento_inicio = aguardar_na_sala(lobby, meu_id, partida)
                if evento_inicio is not None:
                    executar_partida_p2p(meu_id, evento_inicio, rede)
            except (ValueError, cl.ErroLobby) as e:
                print(f"Erro ao criar partida: {e}")

        elif opcao == "3":
            id_str = input("ID da partida: ").strip()
            try:
                id_partida = int(id_str)
                partida = lobby.entrar_partida(id_partida)
                evento_inicio = aguardar_na_sala(lobby, meu_id, partida)
                if evento_inicio is not None:
                    executar_partida_p2p(meu_id, evento_inicio, rede)
            except (ValueError, cl.ErroLobby) as e:
                print(f"Erro ao entrar na partida: {e}")

        elif opcao == "0":
            print("Encerrando...")
            break
        else:
            print("Opção inválida!")

    if not lobby.conectado:
        print("\nServidor de lobby desconectado. Encerrando cliente.")


def main():
    parser = argparse.ArgumentParser(
        description="Cliente do Jogo de Cartas P2P (Uno distribuído)"
    )
    parser.add_argument(
        "--lobby-host",
        default="127.0.0.1",
        help="endereço IP/host do servidor de lobby (padrão: 127.0.0.1)",
    )
    parser.add_argument(
        "--lobby-porta",
        type=int,
        default=5000,
        help="porta TCP do servidor de lobby (padrão: 5000)",
    )
    parser.add_argument(
        "--host-p2p",
        default=None,
        help="IP/host anunciado aos demais pares (opcional; padrão: IP visto pelo lobby)",
    )
    parser.add_argument(
        "--porta-p2p",
        type=int,
        default=0,
        help="porta TCP local para conexões P2P (0 = porta automática)",
    )
    parser.add_argument(
        "--nome",
        default=None,
        help="nome do jogador no lobby",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="ativa logs detalhados de depuração",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    nome = (args.nome or input("Nome do jogador: ")).strip()
    if not nome:
        print("Nome inválido.")
        sys.exit(1)

    # 1. Abre o socket servidor TCP P2P antes de se registrar no lobby
    rede = rede_p2p.RedeP2P(host_escuta="0.0.0.0", porta_escuta=args.porta_p2p)
    porta_real = rede.iniciar_servidor()
    print(f"Socket P2P escutando na porta {porta_real}.")

    # 2. Conecta e registra no servidor de lobby
    try:
        lobby = cl.ClienteLobby(args.lobby_host, args.lobby_porta)
        meu_id = lobby.registrar(nome, porta_p2p=porta_real, host_p2p=args.host_p2p)
        print(f"Registrado no lobby como '{nome}' (id={meu_id}).")
    except (OSError, cl.ErroLobby) as e:
        print(f"Falha ao conectar/registrar no lobby: {e}")
        rede.fechar()
        sys.exit(1)

    try:
        menu_lobby(lobby, meu_id, rede)
    except KeyboardInterrupt:
        print("\nEncerrando cliente...")
    finally:
        rede.fechar()
        lobby.fechar()


if __name__ == "__main__":
    main()
