print("================================")
print("       JOGO DE CARTAS P2P")
print("================================")

# Identificação do jogador
nome = input("Digite seu nome: ")

# Configuração inicial
jogador = {
    "nome": nome,
    "mao": [],
    "conectado": True
}

print("\nJogador cadastrado!")
print(f"Nome: {jogador['nome']}")
print(f"Cartas na mão: {len(jogador['mao'])}")
print(f"Conectado: {jogador['conectado']}")
