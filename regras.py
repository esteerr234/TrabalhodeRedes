
# Verificando se uma carta pode ser jogada

def pode_jogar(carta, carta_mesa):

    if carta[0] == "Coringa":
        return True

    elif carta[0] == carta_mesa[0]:
        return True

    elif carta[1] == carta_mesa[1]:
        return True

    else:
        return False


# Encontrando as cartas válidas da mão

def cartas_validas(mao, carta_mesa):

    validas = []

    for carta in mao:
        if pode_jogar(carta, carta_mesa):
            validas.append(carta)

    return validas


# Aplicando efeitos das cartas especiais

def aplicar_efeito(carta, sentido):

    if carta[1] == "+2":
        print("Efeito +2 ativado!")
        return 2, sentido

    elif carta[1] == "+4":
        print("Efeito +4 ativado!")
        return 4, sentido

    elif carta[1] == "Pular":
        print("Efeito Pular ativado!")
        return 1, sentido

    elif carta[1] == "Inverter":
        print("Sentido da partida invertido!")
        sentido = sentido * -1
        return 0, sentido

    return 0, sentido


# Calculando o próximo jogador

def proximo_jogador(jogador_atual, quantidade_jogadores, sentido):

    return (jogador_atual + sentido) % quantidade_jogadores
