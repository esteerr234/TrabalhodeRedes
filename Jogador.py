def criar_jogador(nome):

    jogador = {
        "nome": nome,
        "mao": [],
        "conectado": True
    }

    return jogador


def mostrar_jogador(jogador):

    print(f"\nNome: {jogador['nome']}")
    print(f"Cartas na mão: {len(jogador['mao'])}")
    print(f"Conectado: {jogador['conectado']}")