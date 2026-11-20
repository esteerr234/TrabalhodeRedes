
import random

# CORES E VALORES DO BARALHO

cores = ["Vermelho", "Amarelo", "Verde", "Azul"]

valores = [
    "0", "1", "2", "3", "4",
    "5", "6", "7", "8", "9",
    "Pular", "Inverter", "+2"
]


# Criando o baralho

def criar_baralho():

    baralho = []

    for cor in cores:
        for valor in valores:
            for copia in range(2):
                baralho.append((cor, valor))

    # Adicionando os coringas
    for copia in range(4):
        baralho.append(("Coringa", "Mudar Cor"))
        baralho.append(("Coringa", "+4"))

    random.shuffle(baralho)

    return baralho


# Distribuindo as cartas

def distribuir_cartas(baralho, quantidade_jogadores):

    jogadores = []

    for i in range(quantidade_jogadores):
        mao = []

        for carta in range(7):
            mao.append(baralho.pop())

        jogadores.append(mao)

    return jogadores


# Comprando cartas e reaproveitando o descarte

def comprar_carta(baralho, pilha_descarte):

    if len(baralho) == 0:

        if len(pilha_descarte) <= 1:
            print("Não existem mais cartas para comprar!")
            return None

        print("\nReorganizando o monte de compras...")

        carta_mesa = pilha_descarte[-1]

        baralho.extend(pilha_descarte[:-1])

        pilha_descarte.clear()
        pilha_descarte.append(carta_mesa)

        random.shuffle(baralho)

    if len(baralho) > 0:
        return baralho.pop()

    return None
