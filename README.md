# Uno P2P — Jogo de Cartas Distribuído sem Servidor Confiável

## Sobre o Projeto

Este projeto implementa um **jogo de cartas multijogador por turnos em rede** (inspirado no Uno).

Foi projetado utilizando uma **arquitetura híbrida** com **servidor de lobby** para descoberta de participantes e comunicação **totalmente peer-to-peer (P2P)** durante a partida, sem nenhum servidor central confiável controlando o estado do jogo.

> [!NOTE]
> Toda a comunicação em rede foi desenvolvida em **Python puro**, utilizando diretamente a API padrão de **sockets TCP**, sem uso de frameworks de rede, bibliotecas de RPC, middlewares de mensageria ou WebSockets.

---

## Requisitos Atendidos e Arquitetura

### Arquitetura Híbrida (Lobby + Full Mesh P2P)

O sistema separa claramente as fases de **descoberta** e de **execução da partida**:

#### 1. Servidor de Lobby (`servidor_lobby.py`):

   - Registra jogadores (nome único, IP e porta TCP onde cada par escuta conexões P2P).
   - Permite listar, criar, entrar e sair de salas (de **3 a 4 jogadores**).
   - Quando o criador inicia a sala (`INICIAR_PARTIDA`), o lobby envia a todos os membros a notificação `PARTIDA_INICIADA` contendo `id_partida`, uma `chave_partida` aleatória (16 bytes em hex) e a lista ordenada de pares (`id_jogador`, `nome`, `host`, `porta`), **removendo a partida do lobby em seguida**.
   - O lobby não embaralha, não valida jogadas e não guarda estado da partida. Por isso, **a queda ou o encerramento do servidor de lobby não interrompe partidas já em andamento**.

#### 2. Malha Completa P2P (`rede_p2p.py`):

   - Cada jogador abre seu socket servidor TCP local **antes** de se registrar no lobby.
   - Ao receber `PARTIDA_INICIADA`, os pares formam uma malha completa (*Full Mesh*): todos conectados diretamente a todos.
   - **Regra determinística anti-duplicação:** para evitar conexões simultâneas duplicadas, o par com `id_jogador` **maior** inicia a conexão TCP para os pares com `id_jogador` **menor** e envia o handshake `OLA`; os pares menores aceitam a conexão, validam `id_partida` e `chave_partida`, e respondem `OLA_OK` (ou `ERRO` com `CHAVE_INVALIDA` e fecham o socket).

### Protocolo de Transporte (TCP) e Enquadramento

- **Escolha do TCP (`SOCK_STREAM`):** por se tratar de um jogo por turnos com estado replicado e embaralhamento criptográfico, a perda ou reordenação de uma única mensagem causaria divergência irreversível de estado. O TCP fornece entrega confiável, ordenada por conexão e controle de fluxo nativamente.
- **Enquadramento NDJSON (`protocolo.py`):** como o TCP é orientado a fluxo de bytes, cada mensagem é serializada como um único objeto JSON UTF-8 terminado por `\n` (limite máximo de `64 KiB` por mensagem). A classe `Conexao` acumula bytes em buffer e entrega apenas mensagens completas, com envio protegido por `threading.Lock`.
- **Ordenação Causal (`seq`):** em uma malha *Full Mesh*, a ação `seq = k + 1` do jogador B pode chegar ao jogador C antes da ação `seq = k` do jogador A, pois trafegam em sockets TCP independentes. O motor `JogoP2P` inclui um contador sequencial `seq` em todas as ações (`ACAO_JOGAR`, `ACAO_COMPRAR`, `ACAO_JOGAR_COMPRADA`, `ACAO_PASSAR`) e mantém um buffer de ações futuras (`_acoes_futuras`), garantindo que todos os pares apliquem as jogadas exatamente na mesma ordem causal.

### Embaralhamento Justo Distribuído (Commit-Reveal com SHA-256)

Nenhum par consegue escolher ou prever a ordem do baralho isoladamente. O protocolo **Commit-Reveal** é executado tanto no **embaralhamento inicial** (rodada 1) quanto na **reciclagem da pilha de descarte** quando o monte de compra acaba (rodadas 2+):

1. **Commit:** cada par ativo gera uma semente aleatória criptograficamente segura (`secrets.token_hex(16)`) e envia em broadcast `COMMIT_SEMENTE` contendo `rodada`, `id_jogador` e `SHA-256(semente)`.
2. **Reveal:** somente após receber `COMMIT_SEMENTE` de **todos** os pares ativos, cada par envia em broadcast `REVELAR_SEMENTE` com a semente em texto claro.
3. **Validação:** cada par verifica se `SHA-256(semente) == commit` recebido anteriormente; em caso de divergência, rejeita com `ERRO(SEMENTE_INVALIDA)`.
4. **Semente Combinada Determinística:** as sementes validadas são ordenadas por `id_jogador`, concatenadas no formato `"id1:semente1|id2:semente2|..."` e passadas por `SHA-256` para gerar um inteiro usado em uma instância isolada `random.Random(semente_final).shuffle(baralho)` sobre o baralho em ordem canônica. Todos os pares chegam exatamente à mesma permutação.

### Validação Independente e Tolerância a Falhas

- **Validação independente:** todos os pares replicam o estado completo da partida e validam cada ação recebida contra `regras.py` (se é a vez do remetente, se a carta pertence à mão dele, se combina com o topo do descarte e se a compra só ocorre sem cartas válidas). Violações são rejeitadas com `ERRO` (`FORA_DE_TURNO`, `JOGADA_INVALIDA`, `ESTADO_INVALIDO`) sem alterar o estado.
- **Detecção de quedas:** a camada P2P envia heartbeats periódicos `PING`/`PONG` (a cada `3 s`, com timeout de socket de `10 s`), além de tratar `TCHAU` (saída voluntária) e fechamento abrupto de conexão (`ConexaoFechada`).
- **Tratamento de desconexão:**
  - O jogador desconectado é removido da lista de participantes ativos (`ativos`).
  - **Destino das cartas:** as cartas da mão do jogador removido são **descartadas fora do jogo**, preservando a ordem determinística do monte de compra e o topo do descarte sem exigir novo embaralhamento imediato.
  - Se a queda ocorrer durante um *Commit-Reveal*, a espera é desbloqueada automaticamente removendo o par caído.
  - Se era a vez do jogador que caiu, o turno avança de forma determinística para o próximo jogador ativo no sentido atual.
  - Se restar apenas **1 jogador ativo**, a partida encerra imediatamente declarando-o vencedor.

---

## Regras do Jogo Implementadas

1. **Baralho (104 cartas):** 4 cores (`Vermelho`, `Amarelo`, `Verde`, `Azul`) × 13 valores (`0` a `9`, `Pular`, `Inverter`, `+2`) × 2 cópias cada. **Não existem cartas Coringa, Mudar Cor ou +4.**
2. **Início:** cada jogador recebe **7 cartas**; uma carta numérica é virada para iniciar a pilha de descarte e as demais formam o monte de compra.
3. **Jogada válida:** na sua vez, o jogador deve jogar uma carta com a **mesma cor** ou o **mesmo número/símbolo** da carta no topo do descarte.
4. **Compra obrigatória (sem compra infinita):** o jogador só compra carta quando **não possui** cartas válidas na mão. Ele compra 1 carta: se puder jogá-la, escolhe jogá-la imediatamente (aplicando eventual efeito especial) ou passar a vez; caso contrário, a vez passa automaticamente.
5. **Cartas especiais:**
   - `Pular`: o próximo jogador não compra cartas e perde a vez.
   - `Inverter`: inverte o sentido da rodada e avança para o próximo jogador no novo sentido.
   - `+2`: o próximo jogador compra 2 cartas e perde a vez.
6. **Reciclagem:** quando o monte de compra acaba, as cartas do descarte (exceto a do topo) são embaralhadas novamente via *Commit-Reveal* para formar um novo monte.
7. **Vitória:** vence o primeiro jogador que ficar sem cartas na mão (ou o último jogador restante em caso de desconexões).

---

## Tecnologias e Restrições

- **Linguagem:** Python 3 (testado em Python 3.10+, compatível com Python 3.8+)
- **Bibliotecas utilizadas (apenas biblioteca padrão do Python):**
  - `socket` (comunicação TCP direta — `AF_INET`, `SOCK_STREAM`)
  - `threading` e `queue` (concorrência e filas seguras entre threads)
  - `json` (serialização NDJSON das mensagens do protocolo)
  - `hashlib` e `secrets` (SHA-256 e geração de sementes/chaves seguras)
  - `random` (embaralhamento determinístico com `random.Random(semente)`)
  - `argparse`, `logging`, `select`, `itertools`, `time`, `sys`
- **Restrições atendidas:** nenhuma dependência externa (`pip`) e **nenhum** framework de rede, biblioteca RPC, middleware de mensageria ou WebSockets.

---

## Estrutura do Projeto

```text
TrabalhodeRedes/
├── baralho.py          # Baralho canônico de 104 cartas e embaralhamento determinístico
├── cliente_lobby.py    # Cliente TCP do servidor de lobby (requisições, fila de eventos e PING)
├── Jogador.py          # Estrutura auxiliar de representação de jogador do protótipo inicial
├── jogo_p2p.py         # Motor de estado replicado, Commit-Reveal, validação e tolerância a falhas
├── main.py             # Ponto de entrada CLI do jogador (menu do lobby + partida P2P no terminal)
├── protocolo.py        # Enquadramento NDJSON sobre TCP (Conexao), tipos de mensagem e códigos de erro
├── rede_p2p.py         # Camada de rede P2P Full Mesh TCP, handshake OLA/OLA_OK e heartbeat
├── regras.py           # Regras do Uno (pode_jogar, cartas_validas, aplicar_efeito, proximo_jogador)
├── servidor_lobby.py   # Servidor TCP concorrente de lobby (registro, salas e descoberta de pares)
├── tests/             # Testes automatizados de regras, protocolo, jogo e integração em rede
└── README.md           # Documentação do projeto
```

---

## 🎮 Como Executar

### Pré-requisitos

- **Python 3** instalado no sistema (não há dependências externas a instalar).

Verifique sua instalação:

```bash
python3 --version
```

### 1. Iniciar o Servidor de Lobby

Abra um terminal na raiz do repositório e inicie o servidor de lobby:

```bash
python3 servidor_lobby.py --host 0.0.0.0 --porta 5000
```

Opções disponíveis em `servidor_lobby.py`:

| Argumento | Padrão | Descrição |
| :--- | :--- | :--- |
| `--host` | `0.0.0.0` | Interface de escuta (`0.0.0.0` aceita conexões de toda a rede local) |
| `--porta` | `5000` | Porta TCP do servidor de lobby |
| `-v`, `--verbose` | desativado | Ativa logs detalhados de depuração |

### 2. Iniciar os Jogadores (Execução Local — 3 ou 4 terminais)

Em **3 ou 4 terminais separados** na mesma máquina, execute `main.py` informando um `--nome` único e uma `--porta-p2p` distinta (ou `0` para alocação automática de porta):

```bash
# Terminal 2 — Jogador 1
python3 main.py --nome Ana --porta-p2p 6001 --lobby-host 127.0.0.1 --lobby-porta 5000

# Terminal 3 — Jogador 2
python3 main.py --nome Bruno --porta-p2p 6002 --lobby-host 127.0.0.1 --lobby-porta 5000

# Terminal 4 — Jogador 3
python3 main.py --nome Carla --porta-p2p 6003 --lobby-host 127.0.0.1 --lobby-porta 5000

# Terminal 5 — Jogador 4 (opcional)
python3 main.py --nome Diego --porta-p2p 6004 --lobby-host 127.0.0.1 --lobby-porta 5000
```

Opções disponíveis em `main.py`:

| Argumento | Padrão | Descrição |
| :--- | :--- | :--- |
| `--lobby-host` | `127.0.0.1` | Endereço IP ou hostname do servidor de lobby |
| `--lobby-porta` | `5000` | Porta TCP do servidor de lobby |
| `--host-p2p` | *(opcional)* | IP/host anunciado aos demais pares (se omitido, o lobby usa o IP de origem da conexão) |
| `--porta-p2p` | `0` | Porta TCP local para conexões P2P (`0` escolhe uma porta livre automaticamente) |
| `--nome` | *(interativo)* | Nome do jogador no lobby |
| `-v`, `--verbose` | desativado | Ativa logs detalhados de depuração |

### 3. Execução em Máquinas Diferentes na Mesma Rede (LAN)

1. **Descubra o IP local** da máquina que executará o lobby (e das máquinas dos jogadores, caso possuam múltiplas interfaces de rede):
   - **Linux:** `ip -4 addr show` ou `hostname -I`
   - **Windows:** `ipconfig`
   - **macOS:** `ipconfig getifaddr en0`
2. **Na máquina do Lobby** (suponha IP `192.168.0.10`):
   ```bash
   python3 servidor_lobby.py --host 0.0.0.0 --porta 5000
   ```
3. **Em cada máquina de jogador** (ex.: `192.168.0.11`, `192.168.0.12`, `192.168.0.13`):
   ```bash
   python3 main.py --nome Ana --lobby-host 192.168.0.10 --lobby-porta 5000 --host-p2p 192.168.0.11 --porta-p2p 6001
   ```
4. **Firewall:** certifique-se de que as portas TCP escolhidas (`5000` na máquina do lobby e `6001`–`6004` nas máquinas dos jogadores) estejam liberadas para tráfego de entrada na rede local.

### 4. Fluxo de Comandos no Terminal

- **Menu do Lobby:**
  - `1` — Listar partidas aguardando jogadores.
  - `2` — Criar partida (informe o nome da sala e o máximo de jogadores: `3` ou `4`).
  - `3` — Entrar em uma partida existente pelo `ID`.
  - `0` — Encerrar o cliente.
- **Dentro da Sala (aguardando início):**
  - `iniciar` (ou `i`) — Disponível apenas para o criador quando houver pelo menos 3 jogadores na sala.
  - `sair` (ou `s`) — Sai da sala e retorna ao menu do lobby.
- **Durante a Partida P2P:**
  - Quando for sua vez e houver cartas válidas: digite o **número da carta** exibido na lista da sua mão (`1` a `N`).
  - Quando não houver cartas válidas: a compra de 1 carta é automática; se a carta comprada for jogável, responda `s` para jogar imediatamente ou `n` para passar a vez.
  - `Ctrl+C` — Envia `TCHAU` aos pares e encerra sua participação na partida.

---

## Como Testar e Cenários de Demonstração

### Testes Automatizados

Os testes estão na pasta `tests/` e utilizam apenas `unittest`, incluído na biblioteca padrão do Python. **O projeto e os testes não utilizam frameworks ou bibliotecas externas e não possuem dependências adicionais ao Python.** Não é necessário instalar pacotes com `pip`.

Na raiz do projeto, execute:

```bash
python3 -m unittest discover -s tests -v
```

Os testes iniciam o lobby e os jogadores automaticamente, usando conexões locais. Ao final, `OK` indica que todos passaram. A suíte verifica as regras, a comunicação e os três cenários principais:

- **Partida completa:** jogadores automáticos jogam até o fim, e todos devem chegar ao mesmo estado e vencedor.
- **Desconexão de jogador:** verifica a saída voluntária e a perda de conexão, o avanço do turno e a vitória do último jogador restante.
- **Queda do lobby:** encerra o servidor durante uma partida e verifica que os jogadores continuam até o fim pela rede P2P.

### Roteiro de Validação Manual ede no mínimo três cenários

Para demonstrar os cenários nos terminais, siga as instruções de execução acima e realize os testes abaixo.

#### Cenário 1 — Partida Completa

1. Inicie `servidor_lobby.py` em um terminal e 3 ou 4 jogadores com `main.py` em terminais separados.
2. Com um jogador, escolha `2` para criar uma sala. Com os demais, escolha `1` para listar e `3` para entrar na sala criada.
3. No terminal do criador, digite `iniciar` e jogue até alguém ficar sem cartas.
4. **Resultado esperado:** todos exibem a mesma carta inicial e ordem de turnos, respeitam as regras de compra e os efeitos das cartas e anunciam o mesmo vencedor. Se o monte acabar, o descarte é reciclado, preservando a carta da mesa.

#### Cenário 2 — Desconexão de um Jogador Durante a Partida

1. Durante uma partida, encerre um jogador com `Ctrl+C` para testar a saída voluntária. Em outra partida, encerre o processo abruptamente (`kill -9 <PID>`) para testar a perda de conexão.
2. Continue jogando com os participantes restantes. Repita a saída até restar apenas um jogador.
3. **Resultado esperado:** os demais detectam a saída e removem o jogador e suas cartas. Se era a vez dele, o turno avança para o próximo participante no sentido atual. O último jogador restante é declarado vencedor.

#### Cenário 3 — Falha do Servidor de Lobby com Partida em Andamento

1. Inicie uma partida com 3 ou 4 jogadores e aguarde o primeiro turno.
2. No terminal do servidor de lobby, pressione `Ctrl+C` para encerrá-lo.
3. Continue jogando até o fim.
4. **Resultado esperado:** a partida continua entre os jogadores, incluindo compras, efeitos especiais e reciclagem do descarte, até todos anunciarem o mesmo vencedor. Após a partida, o cliente informa que o lobby está desconectado.

---

## Identificação Acadêmica

- **Instituição:** Universidade de Brasília (UnB)
- **Departamento:** Departamento de Ciência da Computação (CIC)
- **Disciplina:** Redes de Computadores
- **Professor:** Prof. Roberto Rodrigues-Filho
- **Atividade:** Trabalho Final - Jogo de cartas P2P sem servidor confiável (inspirado no Uno)

---

## Participantes do Projeto

| Nome | Matrícula |
| :--- | :--- |
| Iago Batista Gomes de Carvalho | 251039406 |
| Ester Andrade Sousa | 242012109 |
| Érica Tawany Neres dos Anjos | 241020788 |
| Pedro Augusto da Silva Cirqueira | 231021351 |
| Cecília Teixeira Paranhos | 242027415 |
| Jussara Barbosa Santiago | 242012056 |
| Rafael Henrique Calado de Oliviera | 242035631 |
| `[Nome do Integrante]` | `[Matrícula]` |
