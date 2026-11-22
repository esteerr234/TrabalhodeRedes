
import baralho
import Jogador
import regras

print("================================")
print("       JOGO DE CARTAS P2P")
print("================================")

quantidade_jogadores = int(input("Quantos jogadores? (3 ou 4): "))

if quantidade_jogadores not in [3, 4]:
    print("Escolha apenas 3 ou 4 jogadores!")

else:
    print("\nIniciando partida...")

    cartas = baralho.criar_baralho()
    jogadores = []

    for i in range(quantidade_jogadores):
        nome = input(f"Nome do Jogador {i + 1}: ")
        jogador = Jogador.criar_jogador(nome)
        jogadores.append(jogador)

    maos = baralho.distribuir_cartas(cartas, quantidade_jogadores)

    for i in range(quantidade_jogadores):
        jogadores[i]["mao"] = maos[i]

    pilha_descarte = []

    while True:
        carta_inicial = cartas.pop()

        if carta_inicial[1] not in ["Pular", "Inverter", "+2"]:
            pilha_descarte.append(carta_inicial)
            break

        cartas.insert(0, carta_inicial)

    print("\nJogadores cadastrados!")

    for jogador in jogadores:
        Jogador.mostrar_jogador(jogador)

    print("\nCarta inicial da mesa:", pilha_descarte[-1])
    print("Cartas restantes no baralho:", len(cartas))

    # CONTROLE DA PARTIDA

    jogador_atual = 0
    sentido = 1
    partida_ativa = True

    while partida_ativa:

        jogador = jogadores[jogador_atual]
        carta_mesa = pilha_descarte[-1]

        print("\n================================")
        print("Vez de:", jogador["nome"])
        print("Carta na mesa:", carta_mesa)
        print("================================")

        print("\nSua mão:")

        for i, carta in enumerate(jogador["mao"]):
            print(f"{i + 1} - {carta}")

        validas = regras.cartas_validas(
            jogador["mao"], carta_mesa
        )

        carta_jogada = None

        if len(validas) == 0:
            print("\nVocê não possui cartas válidas.")
            print("Comprando uma carta...")

            nova_carta = baralho.comprar_carta(
                cartas, pilha_descarte
            )

            if nova_carta is not None:
                jogador["mao"].append(nova_carta)
                print("Carta comprada:", nova_carta)

                if regras.pode_jogar(nova_carta, carta_mesa):
                    resposta = input(
                        "Deseja jogar a carta? (s/n): "
                    ).strip().lower()

                    while resposta not in ("s", "n"):
                        resposta = input(
                            "Responda com 's' ou 'n': "
                        ).strip().lower()

                    if resposta == "s":
                        carta_jogada = nova_carta
                    else:
                        print("Você passou a vez.")
                else:
                    print("Carta não pode ser jogada. Passando a vez.")
            else:
                print("Não há cartas disponíveis para comprar. Passando a vez.")

        else:
            print("\nCartas que você pode jogar:")

            for carta in validas:
                print(carta)

            escolha = input(
                "\nDigite o número da carta que deseja jogar: "
            ).strip()

            try:
                indice = int(escolha) - 1

                if indice < 0 or indice >= len(jogador["mao"]):
                    print("Número inválido!")
                    continue

                carta_escolhida = jogador["mao"][indice]

                if regras.pode_jogar(carta_escolhida, carta_mesa):
                    carta_jogada = carta_escolhida
                else:
                    print("Essa carta não pode ser jogada!")
                    continue

            except ValueError:
                print("Digite um número válido!")
                continue

        if carta_jogada is not None:
            jogador["mao"].remove(carta_jogada)
            pilha_descarte.append(carta_jogada)
            print("Carta jogada:", carta_jogada)

            if len(jogador["mao"]) == 0:
                print("\n", jogador["nome"], "venceu a partida!")
                partida_ativa = False
                continue

            cartas_comprar, pular_proximo, sentido = regras.aplicar_efeito(
                carta_jogada, sentido
            )

            proximo_idx = regras.proximo_jogador(
                jogador_atual,
                quantidade_jogadores,
                sentido
            )

            if cartas_comprar > 0:
                proximo = jogadores[proximo_idx]
                for _ in range(cartas_comprar):
                    carta_comprada = baralho.comprar_carta(
                        cartas, pilha_descarte
                    )
                    if carta_comprada is not None:
                        proximo["mao"].append(carta_comprada)
                print(
                    f"{proximo['nome']} comprou {cartas_comprar} cartas "
                    "e perdeu a vez!"
                )
            elif pular_proximo:
                proximo = jogadores[proximo_idx]
                print(f"{proximo['nome']} perdeu a vez!")

            if pular_proximo:
                jogador_atual = regras.proximo_jogador(
                    proximo_idx,
                    quantidade_jogadores,
                    sentido
                )
            else:
                jogador_atual = proximo_idx

        else:
            jogador_atual = regras.proximo_jogador(
                jogador_atual,
                quantidade_jogadores,
                sentido
            )

    print("\nPartida encerrada!")
